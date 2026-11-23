"""Grabwave engine: queue manager, downloader (yt-dlp) and live recorder (streamlink / yt-dlp).

No Qt imports here so it can be tested headless.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, asdict, field
from pathlib import Path
from urllib.parse import urljoin, urlparse

MAX_CONCURRENT = 20  # hard cap (MeGet-style apps typically stop at 15)

# ----------------------------------------------------------------------------
# Paths / config
# ----------------------------------------------------------------------------


def hide_child_windows() -> None:
    """Windows: stop every helper process (ffmpeg, powershell, ...) from opening a console window."""
    if os.name != "nt" or getattr(subprocess.Popen, "_gw_patched", False):
        return
    _orig = subprocess.Popen

    class _QuietPopen(_orig):  # type: ignore[misc, valid-type]
        _gw_patched = True

        def __init__(self, *a, **kw):
            kw["creationflags"] = kw.get("creationflags", 0) | subprocess.CREATE_NO_WINDOW
            try:
                si = kw.get("startupinfo") or subprocess.STARTUPINFO()
                si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                si.wShowWindow = 0  # SW_HIDE
                kw["startupinfo"] = si
            except Exception:
                pass
            super().__init__(*a, **kw)

    subprocess.Popen = _QuietPopen  # type: ignore[misc]


hide_child_windows()


def app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def config_dir() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    d = base / "Grabwave"
    if not d.exists():
        previous = base / ("Vid" + "Grab")  # the app's earlier name: carry over settings, queue and sign-ins
        if previous.is_dir():
            try:
                shutil.copytree(previous, d)
            except OSError:
                pass
    d.mkdir(parents=True, exist_ok=True)
    return d


# ----------------------------------------------------------------------------
# Log file and diagnostics (Help > Copy diagnostics)
# ----------------------------------------------------------------------------
import logging
import logging.handlers

LOG = logging.getLogger("grabwave")


def log_path() -> Path:
    return config_dir() / "grabwave.log"


def _init_log() -> None:
    if LOG.handlers:
        return
    try:
        h = logging.handlers.RotatingFileHandler(log_path(), maxBytes=800_000, backupCount=2, encoding="utf-8")
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        LOG.addHandler(h)
        LOG.setLevel(logging.INFO)
    except Exception:  # noqa: BLE001  (a read-only profile must never stop the app)
        LOG.addHandler(logging.NullHandler())


def diagnostics(version: str = "") -> str:
    """Plain text a person can paste into a message: versions, system, and the end of the log."""
    import platform
    parts = [f"Grabwave {version}", f"{platform.system()} {platform.release()} {platform.machine()}",
             f"Python {platform.python_version()}"]
    for mod in ("yt_dlp", "streamlink", "curl_cffi", "PySide6"):
        try:
            m = __import__(mod)
            v = getattr(getattr(m, "version", m), "__version__", "") or getattr(m, "__version__", "")
            parts.append(f"{mod} {v}")
        except Exception:  # noqa: BLE001
            parts.append(f"{mod} not available")
    parts.append(f"ffmpeg: {'found' if find_ffmpeg() else 'missing'}")
    try:
        lines = log_path().read_text("utf-8", "replace").splitlines()[-150:]
    except Exception:  # noqa: BLE001
        lines = []
    return "\n".join(parts) + "\n\n--- log (last lines) ---\n" + "\n".join(lines)



def _prepare_path() -> None:
    """Let the app find an ffmpeg that sits next to the executable."""
    d = str(app_dir())
    if d not in os.environ.get("PATH", ""):
        os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")


_prepare_path()


# ----------------------------------------------------------------------------
# In-app updates of the yt-dlp engine (sites change often; the installed app can refresh it itself)
# ----------------------------------------------------------------------------

YTDLP_URLS = {
    "nightly": "https://github.com/yt-dlp/yt-dlp-nightly-builds/releases/latest/download/yt-dlp",
    "stable": "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp",
}


def ytdlp_zip() -> Path:
    d = config_dir() / "engine"
    d.mkdir(parents=True, exist_ok=True)
    return d / "yt-dlp.zip"


class _UpdFinder:
    """Makes `import yt_dlp` come from the downloaded yt-dlp zip instead of the copy bundled in the app."""

    def __init__(self, zi):
        self.zi = zi

    def find_spec(self, name, path=None, target=None):
        if name == "yt_dlp" or name.startswith("yt_dlp."):
            return self.zi.find_spec(name)
        return None


def _drop_updfinder() -> None:
    sys.meta_path[:] = [f for f in sys.meta_path if not isinstance(f, _UpdFinder)]
    for m in [m for m in sys.modules if m == "yt_dlp" or m.startswith("yt_dlp.")]:
        del sys.modules[m]


def activate_ytdlp_update() -> str:
    """Call once at startup, before anything imports yt_dlp. Returns the updated version, or '' if none is used."""
    zp = ytdlp_zip()
    if not zp.is_file() or "yt_dlp" in sys.modules:
        return ""
    try:
        import zipimport
        sys.meta_path.insert(0, _UpdFinder(zipimport.zipimporter(str(zp))))
        import yt_dlp
        if str(zp) not in (getattr(yt_dlp, "__file__", "") or ""):
            raise ImportError("update not used")
        from yt_dlp.version import __version__
        return __version__
    except Exception:
        _drop_updfinder()  # fall back to the bundled copy
        try:
            zp.replace(zp.with_suffix(".bad"))
        except OSError:
            pass
        return ""


def ytdlp_version() -> str:
    try:
        from yt_dlp.version import __version__
        return __version__
    except Exception:
        return ""


def update_ytdlp(channel: str = "nightly", proxy: str = "") -> str:
    """Download the newest yt-dlp (it is pure Python) into the user's settings folder. Returns its version.
    Takes effect the next time Grabwave starts."""
    import re
    import ssl
    import urllib.request
    import zipfile

    url = YTDLP_URLS.get(channel, YTDLP_URLS["nightly"])
    try:
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
    except Exception:
        ctx = ssl.create_default_context()
    handlers = [urllib.request.HTTPSHandler(context=ctx)]
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    opener = urllib.request.build_opener(*handlers)
    tmp = ytdlp_zip().with_suffix(".download")
    req = urllib.request.Request(url, headers={"User-Agent": "Grabwave"})
    with opener.open(req, timeout=60) as r, open(tmp, "wb") as f:
        shutil.copyfileobj(r, f)
    try:
        if tmp.stat().st_size < 500_000 or not zipfile.is_zipfile(tmp):
            raise RuntimeError("The downloaded file is not a valid yt-dlp package.")
        with zipfile.ZipFile(tmp) as z:
            m = re.search(r"__version__\s*=\s*['\"]([^'\"]+)", z.read("yt_dlp/version.py").decode("utf-8", "replace"))
        os.replace(tmp, ytdlp_zip())
    finally:
        tmp.unlink(missing_ok=True)
    return m.group(1) if m else "?"


def find_ffmpeg() -> str | None:
    """Look next to the app (and in ./ffmpeg or ./bin) first, then PATH.
    On Windows, ffmpeg.exe loads its DLLs from its own folder, so keep them together."""
    import sys
    name = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    bases = [Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent,
             Path.cwd(), config_dir()]
    for b in bases:
        for sub in ("", "ffmpeg", "bin"):
            c = b / sub / name
            if c.is_file():
                return str(c)
    return shutil.which("ffmpeg")


class _SafeRe:
    """Plain pass-through for urllib3's percent-encoding regex. yt-dlp swaps that regex for its own
    wrapper when its networking loads; if the wrapper ends up stacked twice, '.sub' (used by
    streamlink's HTTP requests) fails. Explicit methods here keep every stacking depth working."""
    def __init__(self, r):
        self._r = r

    @property
    def pattern(self):
        return self._r.pattern

    @property
    def flags(self):
        return self._r.flags


for _n in ("sub", "subn", "match", "search", "fullmatch", "split", "findall", "finditer"):
    setattr(_SafeRe, _n, lambda self, *a, _n=_n, **k: getattr(self._r, _n)(*a, **k))


def fix_urllib3() -> None:
    """Idempotent. Call after yt_dlp's networking has loaded and before using streamlink."""
    try:
        import urllib3.util.url as u
        r = getattr(u, "_PERCENT_RE", None)
        if r is None or isinstance(r, _SafeRe):
            return
        base = r
        for _ in range(8):  # unwrap yt-dlp wrapper(s) down to the real compiled regex
            if hasattr(base, "pattern") and hasattr(base, "sub"):
                break
            nxt = getattr(base, "re", None)
            if nxt is None:
                return
            base = nxt
        u._PERCENT_RE = _SafeRe(base)
    except Exception:
        pass


def builtin_cookie_path() -> Path:
    """Cookies exported by the built-in browser pane (Netscape format)."""
    return config_dir() / "browser_cookies.txt"


def effective_cookie_file(cfg: "Config") -> str:
    """The user's cookies.txt if set, else a private copy of the built-in browser's cookies."""
    import tempfile
    if cfg.cookies_file and Path(cfg.cookies_file).is_file():
        return cfg.cookies_file
    p = builtin_cookie_path()
    if cfg.use_builtin_cookies and p.is_file() and p.stat().st_size > 0:
        fd, tmp = tempfile.mkstemp(suffix=".txt", prefix="cj_", dir=str(config_dir()))
        os.close(fd)
        shutil.copyfile(p, tmp)  # yt-dlp rewrites its cookie file, so give it a copy
        return tmp
    return ""


SUBTITLE_CODES = {"English": "en", "Spanish": "es", "French": "fr", "German": "de",
                  "Portuguese": "pt", "Italian": "it", "Russian": "ru", "Japanese": "ja",
                  "Chinese": "zh", "Korean": "ko", "Arabic": "ar"}


@dataclass
class Config:
    download_dir: str = str(Path.home() / "Downloads" / "Grabwave")
    live_dir: str = str(Path.home() / "Downloads" / "Grabwave" / "Recordings")
    max_concurrent: int = MAX_CONCURRENT
    cookies_file: str = ""
    cookies_browser: str = ""  # firefox / chrome / edge / brave ... ("" = off)
    use_builtin_cookies: bool = True  # reuse sign-ins from Grabwave's own browser pane
    fmt: str = "MP4"          # MP4 / MKV / WEBM / MP3 (audio)
    quality: str = "Best"     # Best / 2160p / 1440p / 1080p / 720p / 480p / 360p
    subtitle: str = "None"
    custom_sites: list = field(default_factory=list)
    proxy: str = ""
    live_quality: str = "best"
    remux_mp4: bool = True
    live_poll_seconds: int = 30
    performance: str = "Balanced"      # Maximum / Balanced / Light
    watchlist: list = field(default_factory=list)  # [{"url","name","enabled","limit_min","limit_mb"}]
    watch_every: int = 120             # seconds between watchlist checks
    close_to_tray: bool = True         # keep running in the tray while the watchlist is on
    smart_paste: bool = True           # offer to grab links copied anywhere
    sounds: bool = True
    tray_collapsed: bool = False
    auto_engine: bool = True           # keep the video engine (yt-dlp) fresh in the background
    engine_checked: float = 0.0
    check_updates: bool = True         # offer new versions when Grabwave opens
    confirm_batch_over: int = 25       # ask before adding more than this many videos from one page (0 = never ask)
    update_url: str = ""               # empty = the built-in address (updater.DEFAULT_MANIFEST_URL)

    @staticmethod
    def path() -> Path:
        return config_dir() / "config.json"

    @classmethod
    def load(cls) -> "Config":
        c = cls()
        try:
            data = json.loads(cls.path().read_text("utf-8"))
            for k, v in data.items():
                if hasattr(c, k):
                    setattr(c, k, v)
        except Exception:
            pass
        c.max_concurrent = max(1, min(MAX_CONCURRENT, int(c.max_concurrent)))
        return c

    def save(self) -> None:
        self.max_concurrent = max(1, min(MAX_CONCURRENT, int(self.max_concurrent)))
        self.path().write_text(json.dumps(asdict(self), indent=2), "utf-8")



# ----------------------------------------------------------------------------
# Performance modes (Settings > Performance)
# ----------------------------------------------------------------------------

PERFORMANCE = {
    # slots: most simultaneous jobs; frags: parallel pieces per download; tick: window refresh (ms);
    # threads: ffmpeg threads (0 = all); thumbs: background picture loaders; prio: process priority;
    # chromium: extra flags for the built-in browser (take effect after a restart)
    "Maximum": dict(slots=MAX_CONCURRENT, frags=16, tick=250, threads=0, thumbs=4, prio="high", chromium=""),
    "Balanced": dict(slots=10, frags=4, tick=500, threads=2, thumbs=2, prio="normal",
                     chromium="--renderer-process-limit=4"),
    "Light": dict(slots=3, frags=1, tick=1000, threads=1, thumbs=1, prio="low",
                  chromium="--disable-gpu --disable-smooth-scrolling --renderer-process-limit=2 "
                           "--disable-background-timer-throttling=false"),
}
PERFORMANCE_NOTES = {
    "Maximum": "Uses everything it needs: up to 20 jobs at once, fast pieces, and a smooth, quick window. "
               "Best on a modern computer. Other programs may feel slower during big batches.",
    "Balanced": "A good mix: up to 10 jobs at once and normal priority. Recommended for most people.",
    "Light": "Gentle on your computer: up to 3 jobs at once, one piece at a time, low priority and a lighter "
             "browser. Good for older laptops or when you are working in other programs.",
}


def perf(cfg: "Config") -> dict:
    return PERFORMANCE.get(cfg.performance, PERFORMANCE["Balanced"])


def effective_slots(cfg: "Config") -> int:
    return max(1, min(cfg.max_concurrent, perf(cfg)["slots"]))


def apply_priority(cfg: "Config") -> None:
    """Ask the operating system to run Grabwave at the chosen priority."""
    level = perf(cfg)["prio"]
    try:
        if os.name == "nt":
            import ctypes
            cls = {"high": 0x00008000, "normal": 0x00000020, "low": 0x00004000}[level]  # ABOVE/NORMAL/BELOW
            ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), cls)
        else:
            want = {"high": 0, "normal": 0, "low": 10}[level]
            cur = os.nice(0)
            if want > cur:  # unprivileged processes can only become nicer
                os.nice(want - cur)
    except Exception:
        pass


# ----------------------------------------------------------------------------
# Items
# ----------------------------------------------------------------------------

RUNNING_STATES = ("Downloading", "Recording", "Waiting", "Processing")


class Item:
    def __init__(self, url: str, kind: str = "download", outdir: str = ""):
        self.id = uuid.uuid4().hex
        self.url = url
        self.kind = kind  # "download" | "live"
        self.outdir = outdir
        self.referer = ""   # sent as the Referer header (direct media URLs found in the browser pane)
        self.filename = ""  # preferred file name (without extension)
        self.alt_url = ""      # stream address the browser pane saw the page request; tried if the page URL fails
        self.alt_referer = ""  # page that requested it (sent as Referer/Origin)
        self.thumb = ""        # thumbnail address reported by the site
        self.duration = 0      # seconds, when known
        self.quality = ""      # per-item quality override ("" = use the main setting)
        self.limit_min = 0     # live: stop recording after this many minutes (0 = no limit)
        self.limit_mb = 0      # live: stop after this many MB (0 = no limit)
        self.title = url
        self.status = "Queued"
        self.done = 0
        self.total = 0
        self.speed = 0.0
        self.eta: int | None = None
        self.path = ""
        self.error = ""
        self.added = time.time()
        self.started = 0.0
        # runtime only
        self.running = False
        self.stop = threading.Event()
        self.reason = ""
        self._tmp = ""
        self._final = ""
        self._dom_html = ""
        self.attempts = 0      # automatic second tries used
        self.retry_at = 0.0    # when the next automatic try starts
        self._pending = None  # result of looking for the videos on a collection page

    def to_json(self) -> dict:
        keys = ("id", "url", "kind", "outdir", "referer", "filename", "title", "status", "done", "total",
                "path", "error", "added", "alt_url", "alt_referer", "thumb", "duration", "quality",
                "limit_min", "limit_mb")
        return {k: getattr(self, k) for k in keys}

    @classmethod
    def from_json(cls, d: dict) -> "Item":
        it = cls(d["url"], d.get("kind", "download"), d.get("outdir", ""))
        for k, v in d.items():
            if hasattr(it, k):
                setattr(it, k, v)
        if it.status == "Retrying":
            it.status = "Failed"
        if it.status == "Retry later":  # closed while waiting to retry: start it again
            it.status = "Queued"
        if it.status == "Checking":  # closed while looking for videos: just download the link normally
            it.status = "Queued"
        if it.status in RUNNING_STATES:  # app was closed while running
            it.status = "Stopped" if it.kind == "download" else "Completed"
            if it.kind == "download":
                it.status = "Paused"
        return it


class StopRequested(Exception):
    pass


class _NoPlugin(Exception):
    pass


# ----------------------------------------------------------------------------
# Workers
# ----------------------------------------------------------------------------

def _common_ydl_opts(cfg: Config, use_cookies: bool = True, quality: str = "") -> dict:
    o = {
        "quiet": True,
        "color": {"stdout": "never", "stderr": "never"},
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        "continuedl": True,
        "retries": 10,
        "fragment_retries": 10,
        "concurrent_fragment_downloads": perf(cfg)["frags"],
        "windowsfilenames": True,
        "trim_file_name": 140,
    }
    cf = effective_cookie_file(cfg) if use_cookies else ""
    if cf:
        o["cookiefile"] = cf
    if use_cookies and cfg.cookies_browser:
        o["cookiesfrombrowser"] = (cfg.cookies_browser,)
    if cfg.proxy:
        o["proxy"] = cfg.proxy
    ff = find_ffmpeg()
    if ff:
        o["ffmpeg_location"] = str(Path(ff).parent)

    # format / quality chosen in the main window
    qual = quality or cfg.quality
    q = qual[:-1] if qual.endswith("p") else ""
    h = int(q) if q.isdigit() else None
    fmt = (cfg.fmt or "MP4").split()[0].lower()
    if fmt == "mp3":
        o["format"] = "bestaudio/best"
        o["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}]
    elif ff:
        o["format"] = f"bv*[height<={h}]+ba/b[height<={h}]/b" if h else "bv*+ba/b"
        o["merge_output_format"] = fmt if fmt in ("mp4", "mkv", "webm") else "mp4"
    else:
        # without ffmpeg we can't merge separate video+audio, so ask for single-file formats
        o["format"] = f"best[height<={h}]/best" if h else "best"
    th = perf(cfg)["threads"]
    if th:
        o["postprocessor_args"] = {"ffmpeg": ["-threads", str(th)]}
    code = SUBTITLE_CODES.get(cfg.subtitle)
    if code:
        o["writesubtitles"] = True
        o["subtitleslangs"] = [code]
    # Many sites sit behind Cloudflare and reject non-browser TLS fingerprints.
    # If curl_cffi is installed, make yt-dlp look like a real Chrome browser.
    try:
        import curl_cffi  # noqa: F401
        from yt_dlp.networking.impersonate import ImpersonateTarget
        o["impersonate"] = ImpersonateTarget("chrome")
    except Exception:
        pass
    return o


def _make_hook(item: Item):
    def hook(d):
        if item.stop.is_set():
            raise StopRequested()
        info = d.get("info_dict") or {}
        if info.get("title") and item.title == item.url:
            item.title = info["title"]
        if info.get("thumbnail") and not item.thumb:
            item.thumb = info["thumbnail"]
        if info.get("duration") and not item.duration:
            item.duration = int(info["duration"])
        item._tmp = d.get("tmpfilename") or item._tmp
        item._final = d.get("filename") or item._final
        if d.get("status") == "downloading":
            item.done = d.get("downloaded_bytes") or 0
            item.total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            item.speed = d.get("speed") or 0.0
            item.eta = d.get("eta")
        elif d.get("status") == "finished":
            item.path = d.get("filename") or item.path
    return hook


def _normalize_url(url: str) -> str:
    """X/Twitter share links often end in /video/1 or /photo/1, which can make yt-dlp say 'Video #1 is
    unavailable'. The plain tweet address works for every video in the post."""
    import re
    p = urlparse(url)
    host = (p.hostname or "").lower()
    if host.endswith(("x.com", "twitter.com", "fxtwitter.com", "vxtwitter.com")):
        path = re.sub(r"/(video|photo)/\d+/?$", "", p.path)
        return f"{p.scheme}://{p.netloc}{path}"
    return url


# ----------------------------------------------------------------------------
# Best quality of a captured stream address
# ----------------------------------------------------------------------------
_HEIGHTS = (2160, 1440, 1080, 720, 480, 360, 240)


def _url_height(u: str) -> int:
    """Picture height a stream address announces (".../720p/...", "_1080.mp4", "1280x720"), 0 if it says nothing."""
    p = u.split("?")[0].lower()
    m = re.search(r"(?<!\d)(\d{3,4})p(?![a-z])", p) or re.search(r"\d{3,4}x(\d{3,4})", p) or \
        re.search(r"[/_=.,-](2160|1440|1080|720|480|360|240)(?=[/_.,&=-]|$)", p)
    return int(m.group(1)) if m else 0


def _quick_get(url: str, referer: str = "") -> str:
    return _page_html(url, referer, None, 6)


def _best_in_master(text: str, base: str, cap: int = 0) -> tuple:
    """(height, bandwidth, address) of the best variant of a master playlist; honours a height cap."""
    vs = []
    lines = text.splitlines()
    for i, ln in enumerate(lines):
        if not ln.startswith("#EXT-X-STREAM-INF"):
            continue
        res = re.search(r"RESOLUTION=(\d+)x(\d+)", ln)
        bw = re.search(r"(?:AVERAGE-)?BANDWIDTH=(\d+)", ln)
        uri = next((x.strip() for x in lines[i + 1:i + 4] if x.strip() and not x.startswith("#")), "")
        if uri:
            h = int(res.group(2)) if res else _url_height(uri)
            vs.append((h, int(bw.group(1)) if bw else 0, urljoin(base, uri)))
    if cap:
        ok = [v for v in vs if v[0] and v[0] <= cap]
        vs = ok or vs
    return max(vs, key=lambda v: (v[0], v[1])) if vs else (0, 0, "")


def _is_playlist(text: str) -> bool:
    return text.lstrip().startswith("#EXTM3U")


def upgrade_stream(url: str, referer: str = "", cap: int = 0) -> str:
    """A better-quality address for the same video, or the address itself. A page's own player often starts on a
    low quality, so the address captured from it may be a small variant. Looks for the master playlist, and for
    the same address with a higher resolution in it (".../480p/..." -> ".../1080p/...")."""
    try:
        return _upgrade_stream(url, referer, cap) or url
    except Exception as ex:  # noqa: BLE001
        LOG.info("upgrade_stream failed for %s: %s", url, str(ex)[:150])
        return url


def _upgrade_stream(url: str, referer: str, cap: int) -> str:
    from concurrent.futures import ThreadPoolExecutor
    base = url.split("?")[0]
    query = url[len(base):]
    low = base.lower()
    h0 = _url_height(url)
    is_pl = low.endswith(".m3u8")
    is_file = low.endswith((".mp4", ".webm", ".m4v", ".mov"))
    if not (is_pl or is_file):
        return url
    if is_pl:
        text = _quick_get(url, referer)
        if "#EXT-X-STREAM-INF" in text:  # a master playlist: the downloader picks its best variant and keeps the audio
            return url
    # candidates: the same address with a larger height in it, and master playlists next to it
    cands = []
    tok = None
    for mm in re.finditer(r"(?<!\d)(2160|1440|1080|720|480|360|240)(?!\d)", base):
        tok = mm  # the last announced height in the address
    if tok is not None:
        cur = int(tok.group(1))
        for H in _HEIGHTS:
            if H > cur and (not cap or H <= cap):
                cands.append((H, base[:tok.start()] + str(H) + base[tok.end():] + query))
    if is_pl:
        d1 = base.rsplit("/", 1)[0]
        d2 = d1.rsplit("/", 1)[0] if "/" in d1[8:] else d1
        for d in dict.fromkeys([d1, d2]):
            for nm in ("master.m3u8", "index.m3u8", "playlist.m3u8", "master_playlist.m3u8"):
                cu = f"{d}/{nm}{query}"
                if cu != url:
                    cands.append((0, cu))

    def check(c):
        H, cu = c
        try:
            if is_pl:
                t = _quick_get(cu, referer)
                if not _is_playlist(t):
                    return None
                if "#EXT-X-STREAM-INF" in t:
                    h, bw, _best = _best_in_master(t, cu, cap)
                    return (h or 1, bw, cu)  # the master itself, so separate audio is kept
                return (H, 0, cu)
            t = _quick_get_head(cu, referer)  # a file: does the larger one exist?
            return (H, t, cu) if t > 100_000 else None
        except Exception:  # noqa: BLE001
            return None

    if not cands:
        return url
    with ThreadPoolExecutor(max_workers=6) as ex:
        found = [r for r in ex.map(check, cands[:14]) if r]
    found = [r for r in found if r[0] > h0 or (is_pl and r[0] >= 1 and h0 == 0)]
    if not found:
        return url
    best = max(found, key=lambda r: (r[0], r[1]))
    LOG.info("better stream found: %s -> %s", url, best[2])
    return best[2]


def _quick_get_head(url: str, referer: str = "") -> int:
    """Size in bytes of a direct file (0 if it is missing or not a video)."""
    import ssl
    import urllib.request
    try:
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
    except Exception:  # noqa: BLE001
        ctx = ssl.create_default_context()
    req = urllib.request.Request(url, headers={"Range": "bytes=0-0", "Referer": referer or url,
                                               "User-Agent": browser_ua() or "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=6, context=ctx) as r:
        ct = (r.headers.get("Content-Type") or "").lower()
        if ct and not (ct.startswith("video/") or "octet-stream" in ct or "mpegurl" in ct):
            return 0
        cr = r.headers.get("Content-Range", "")
        if "/" in cr and cr.split("/")[-1].isdigit():
            return int(cr.split("/")[-1])
        return int(r.headers.get("Content-Length") or 0)


def _quality_cap(item, cfg) -> int:
    q = (getattr(item, "quality", "") or cfg.quality or "")
    q = q[:-1] if q.endswith("p") else ""
    return int(q) if q.isdigit() else 0


def run_download(item: Item, cfg: Config) -> None:
    """Try the page/media URL with yt-dlp; then again without the browser cookies (stale sign-ins break some
    sites, e.g. X/Twitter); then the stream address the browser pane captured (item.alt_url)."""
    def reset():
        item.error = ""
        item.done = item.total = 0
        item.speed = 0.0

    first = None
    url = _normalize_url(item.url)
    try:
        _download_once(item, cfg, url, item.referer, False)
        return
    except Exception as ex:
        if item.stop.is_set():
            raise
        first = ex
    if effective_cookie_file(cfg) or cfg.cookies_browser:
        reset()
        try:
            _download_once(item, cfg, url, item.referer, False, use_cookies=False)
            return
        except Exception:
            if item.stop.is_set():
                raise
    # sites built on the "KVS" video script: read the video addresses straight from the page
    # (also used when yt-dlp trips over the page, e.g. "Unable to extract flashvars")
    if item.kind == "download" and url.lower().startswith(("http://", "https://")):
        try:
            cands = kvs_candidates(url, item.referer)
        except Exception:  # noqa: BLE001
            cands = []
        for cand in cands:
            if item.stop.is_set():
                break
            reset()
            try:
                _download_once(item, cfg, cand, url, True)
                return
            except Exception:  # noqa: BLE001
                if item.stop.is_set():
                    raise
    if item.alt_url:
        ref = item.alt_referer or item.referer or item.url
        best = upgrade_stream(item.alt_url, ref, _quality_cap(item, cfg))  # the page's player may have been on a low quality
        for addr in dict.fromkeys([best, item.alt_url]):
            reset()
            try:
                _download_once(item, cfg, addr, ref, True)
                return
            except Exception:  # noqa: BLE001
                if item.stop.is_set():
                    raise
            reset()
            try:  # second way for a stream address: let ffmpeg copy it straight into a file
                _ffmpeg_copy(item, cfg, addr, ref)
                return
            except Exception:  # noqa: BLE001
                if item.stop.is_set():
                    raise
    # last resort: gallery-dl knows many picture / video sites that yt-dlp doesn't
    if item.kind == "download" and url.lower().startswith(("http://", "https://")):
        reset()
        try:
            _gallery_dl(item, cfg, url)
            return
        except Exception:  # noqa: BLE001
            if item.stop.is_set():
                raise
    raise first


def _ffmpeg_copy(item: Item, cfg: Config, url: str, referer: str) -> None:
    """Save a stream address (playlist or file) with ffmpeg, copying without re-encoding."""
    ff = find_ffmpeg()
    if not ff:
        raise RuntimeError("ffmpeg not found")
    outdir = Path(item.outdir or cfg.download_dir)
    outdir.mkdir(parents=True, exist_ok=True)
    base = re.sub(r"[^\w\-. ]+", "_", (item.filename or item.title or "video"))[:90].strip() or "video"
    out = outdir / f"{base} [ffmpeg].mp4"
    tmp = config_dir() / "tmp" / f"{item.id}.mp4"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    hdrs = f"Referer: {referer}\r\nUser-Agent: {browser_ua() or 'Mozilla/5.0'}\r\n"
    cmd = [ff, "-y", "-loglevel", "error", "-headers", hdrs, "-i", url, "-c", "copy",
           "-bsf:a", "aac_adtstoasc", "-movflags", "+faststart", str(tmp)]
    if cfg.proxy:
        cmd[1:1] = ["-http_proxy", cfg.proxy]
    item.status = "Downloading"
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    while p.poll() is None:
        if item.stop.is_set():
            p.kill()
            raise StopRequested()
        try:
            item.done = tmp.stat().st_size
        except OSError:
            pass
        time.sleep(0.5)
    if p.returncode != 0 or not tmp.exists() or tmp.stat().st_size < 5_000:
        err = (p.stderr.read() if p.stderr else "")[-300:]
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"ffmpeg could not save the stream. {err}")
    shutil.move(str(tmp), str(out))
    item.path = str(out)
    item.title = item.title or base
    item.total = item.done = out.stat().st_size


def _gallery_dl(item: Item, cfg: Config, url: str) -> None:
    """Ask gallery-dl (if it is installed) to fetch the page's media, then keep the biggest video file."""
    try:
        from gallery_dl import config as gconf, job as gjob
    except Exception:
        raise RuntimeError("gallery-dl is not available")
    outdir = Path(item.outdir or cfg.download_dir)
    outdir.mkdir(parents=True, exist_ok=True)
    work = config_dir() / "tmp" / f"{item.id}-gdl"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    gconf.clear()
    gconf.set((), "base-directory", str(work))
    gconf.set((), "directory", [])
    gconf.set(("extractor",), "filename", "{filename}.{extension}")
    cf = effective_cookie_file(cfg)
    if cf:
        gconf.set(("extractor",), "cookies", cf)
    if cfg.proxy:
        gconf.set(("extractor",), "proxy", cfg.proxy)
    item.status = "Downloading"
    gjob.DownloadJob(url).run()
    files = [f for f in work.rglob("*") if f.is_file() and f.stat().st_size > 20_000]
    videos = [f for f in files if f.suffix.lower() in (".mp4", ".webm", ".mkv", ".mov", ".m4v", ".gif", ".mp3", ".m4a")]
    pick = max(videos or files, key=lambda f: f.stat().st_size, default=None)
    if pick is None:
        shutil.rmtree(work, ignore_errors=True)
        raise RuntimeError("gallery-dl found nothing to save")
    dest = outdir / pick.name
    n = 1
    while dest.exists():
        dest = outdir / f"{pick.stem} ({n}){pick.suffix}"
        n += 1
    shutil.move(str(pick), str(dest))
    shutil.rmtree(work, ignore_errors=True)
    item.path = str(dest)
    item.title = item.title or dest.stem
    item.total = item.done = dest.stat().st_size


def _kvs_token(license_code: str) -> list:
    code = license_code.replace("$", "")
    vals = [int(c) for c in code]
    mod = code.replace("0", "1")
    center = len(mod) // 2
    front, back = int(mod[:center + 1]), int(mod[center:])
    mod = str(4 * abs(front - back))[:center + 1]
    return [(vals[i + off] + int(cur)) % 10 for i, cur in enumerate(mod) for off in range(4)]


def _kvs_real_url(video_url: str, license_code: str) -> str:
    """Addresses that start with "function/0/" have 32 letters of their path scrambled; undo it."""
    if not video_url.startswith("function/0/"):
        return video_url
    if not license_code:
        return ""
    url = video_url[len("function/0/"):]
    parts = url.split("/")
    # the scrambled part is the 32-character hash right after ".../get_file/<number>/"
    idx = next((i for i, p in enumerate(parts) if len(p) >= 32 and re.fullmatch(r"[0-9a-f]{32}.*", p)), -1)
    if idx < 0:
        return ""
    token = _kvs_token(license_code)
    hlen = 32
    h = parts[idx][:hlen]
    order = list(range(hlen))
    acc = 0
    for src in reversed(range(hlen)):
        acc += token[src]
        dest = (src + acc) % hlen
        order[src], order[dest] = order[dest], order[src]
    parts[idx] = "".join(h[i] for i in order) + parts[idx][hlen:]
    return "/".join(parts)


def kvs_candidates(page_url: str, referer: str = "") -> list:
    """Video addresses found in the page's player settings, best quality first. Empty if the page isn't one."""
    headers = {"User-Agent": browser_ua() or "Mozilla/5.0", "Referer": referer or page_url,
               "Accept-Language": "en-US,en;q=0.9"}
    html = ""
    try:
        from curl_cffi import requests as cr
        html = cr.get(page_url, headers=headers, impersonate="chrome", timeout=25).text
    except Exception:
        try:
            import ssl
            import urllib.request
            try:
                import certifi
                ctx = ssl.create_default_context(cafile=certifi.where())
            except Exception:
                ctx = ssl.create_default_context()
            with urllib.request.urlopen(urllib.request.Request(page_url, headers=headers), timeout=25,
                                        context=ctx) as r:
                html = r.read(3_000_000).decode("utf-8", "replace")
        except Exception:
            return []
    if "get_file" not in html and "flashvars" not in html:
        return []
    lic = re.search(r"license_code\s*:\s*['\"]([^'\"]+)['\"]", html)
    lic = lic.group(1) if lic else ""
    urls = {m.group(1): m.group(2) for m in
            re.finditer(r"\b(video(?:_alt)?_url\d*)\s*:\s*['\"]([^'\"]+)['\"]", html)}
    labels = {m.group(1): m.group(2) for m in
              re.finditer(r"\b(video(?:_alt)?_url\d*)_text\s*:\s*['\"]([^'\"]+)['\"]", html)}
    out = []
    for key, raw in urls.items():
        real = _kvs_real_url(raw.strip(), lic)
        if not real.lower().startswith(("http://", "https://")):
            continue
        m = re.search(r"(\d{3,4})\s*p", labels.get(key, "") + " " + real)
        out.append((int(m.group(1)) if m else 0, real))
    out.sort(key=lambda t: -t[0])
    seen, res = set(), []
    for _q, u in out:
        if u not in seen:
            seen.add(u)
            res.append(u)
    return res


def _download_once(item: Item, cfg: Config, url: str, referer: str, is_alt: bool, use_cookies: bool = True) -> None:
    import yt_dlp

    outdir = item.outdir or cfg.download_dir
    Path(outdir).mkdir(parents=True, exist_ok=True)
    # Work in a folder on the local drive and move the finished file to the chosen folder. Network/cloud
    # drives (and very long paths) often fail on the many small temporary pieces a download creates.
    tmpdir = config_dir() / "tmp" / item.id
    tmpdir.mkdir(parents=True, exist_ok=True)
    opts = _common_ydl_opts(cfg, use_cookies, item.quality)
    opts["paths"] = {"home": str(outdir), "temp": str(tmpdir)}
    default_tmpl = "%(title).110B [%(id).24B].%(ext)s"  # short: Windows paths max out at 260
    opts["outtmpl"] = default_tmpl
    if item.filename:
        opts["outtmpl"] = item.filename.replace("%", "%%") + ".%(ext)s"
    if referer:
        hdr = {"Referer": referer}
        if is_alt:
            p = urlparse(referer)
            if p.scheme and p.netloc:
                hdr["Origin"] = f"{p.scheme}://{p.netloc}"
        opts["http_headers"] = hdr
    opts["progress_hooks"] = [_make_hook(item)]
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
    except Exception as e:  # noqa: BLE001
        # file-name trouble (odd characters, emoji, a drive that refuses the name): retry with a plain name
        msg = str(e)
        if item.stop.is_set() or not ("Errno 2" in msg or "Errno 22" in msg or "No such file" in msg
                                      or "Invalid argument" in msg):
            raise  # (partial files stay in the local work folder so Resume can continue them)
        opts["outtmpl"] = "%(id).40B.%(ext)s"
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
    shutil.rmtree(tmpdir, ignore_errors=True)  # finished: the work folder is no longer needed
    if info and not item.filename:
        item.title = info.get("title") or item.title
    if info:
        rd = info.get("requested_downloads") or []
        if rd and rd[0].get("filepath"):
            item.path = rd[0]["filepath"]
    if item.total:
        item.done = item.total


def _channel_name(url: str) -> str:
    p = urlparse(url)
    parts = [s for s in p.path.split("/") if s]
    name = parts[-1] if parts else p.netloc
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in name)
    return safe or "stream"


def _new_session(cfg: Config):
    """A streamlink session with ffmpeg, proxy and the browser pane's sign-ins applied."""
    from streamlink import Streamlink

    sl = Streamlink()
    ff = find_ffmpeg()
    if ff:
        sl.set_option("ffmpeg-ffmpeg", ff)
    if cfg.proxy:
        sl.set_option("http-proxy", cfg.proxy)
    cookie_file = effective_cookie_file(cfg)
    if cookie_file:
        try:
            import http.cookiejar as cj
            jar = cj.MozillaCookieJar(cookie_file)
            jar.load(ignore_discard=True, ignore_expires=True)
            for c in jar:
                sl.http.cookies.set_cookie(c)
        except Exception:
            pass
    return sl


def check_live(url: str, cfg: Config) -> bool:
    """True if the room/channel is broadcasting publicly right now (used by the watchlist)."""
    try:
        import yt_dlp.networking._requests  # noqa: F401
    except Exception:
        pass
    fix_urllib3()
    try:
        sl = _new_session(cfg)
        if cam_detect(url):
            try:
                cam_open_stream(sl, url, "best", cfg.cookies_browser, effective_cookie_file(cfg))
                return True
            except Exception:
                return False
        return bool(sl.streams(url))
    except Exception:
        return False


def browser_ua() -> str:
    """The built-in browser's User-Agent (saved by the browser pane), so recordings look like the same visitor."""
    try:
        t = (config_dir() / "browser_ua.txt").read_text("utf-8").strip()
        if t:
            return t
    except Exception:
        pass
    return CAM_UA


def _open_alt_hls(sl, url: str, referer: str, quality: str):
    """Open the playlist the browser pane captured, sending the same Referer/Origin/User-Agent."""
    from streamlink.stream.hls import HLSStream
    p = urlparse(referer)
    headers = {"Referer": referer, "User-Agent": browser_ua()}
    if p.scheme and p.netloc:
        headers["Origin"] = f"{p.scheme}://{p.netloc}"
    sl.set_option("http-headers", headers)
    if ".m3u8" in url.lower():
        streams = HLSStream.parse_variant_playlist(sl, url, headers=headers)
        if streams:
            return _cam_pick(dict(streams), quality)
    return HLSStream(sl, url, headers=headers)


def run_live_streamlink(item: Item, cfg: Config) -> None:
    from streamlink import Streamlink
    from streamlink.exceptions import NoPluginError, PluginError

    sl = _new_session(cfg)
    cookie_file = effective_cookie_file(cfg)

    stream = None
    while stream is None:
        if item.stop.is_set():
            item.status = "Stopped"
            raise StopRequested()
        why = "waiting for stream"
        if cam_detect(item.url):
            try:
                stream = cam_open_stream(sl, item.url, cfg.live_quality, cfg.cookies_browser, cookie_file)
            except CamOffline as e:
                why = f"offline/not public: {e}"
            except Exception as e:  # site blocked our direct lookup (e.g. HTTP 418)
                why = f"blocked by the site: {str(e)[:70]}"
                # 1) the stream address the built-in browser saw while the room was playing
                if item.alt_url:
                    try:
                        stream = _open_alt_hls(sl, item.alt_url, item.alt_referer or item.url, cfg.live_quality)
                    except Exception as e2:  # noqa: BLE001
                        why = f"saved stream address failed: {str(e2)[:60]}"
                # 2) streamlink's own plugin
                if stream is None:
                    try:
                        streams = sl.streams(item.url)
                        if streams:
                            stream = streams.get(cfg.live_quality) or streams.get("best")
                    except Exception:
                        pass
                if stream is None and not item.alt_url:
                    item.error = ("The site refused Grabwave's direct lookup (HTTP 418 means it blocks automated "
                                  "requests).\n\nGrabwave is now opening the room page itself, like a browser, to "
                                  "pick up the stream address, and tries again every 90 seconds while the room is "
                                  "offline. If it never starts: open the room in the Browse tab, sign in if the "
                                  "site asks, then click the download button (top right of the browser bar).")
        else:
            try:
                streams = sl.streams(item.url)
            except NoPluginError:
                raise _NoPlugin()
            except PluginError:
                streams = {}
            if streams:
                stream = streams.get(cfg.live_quality) or streams.get("best")
        if stream is None:
            item.status = "Waiting"
            item.title = f"{_channel_name(item.url)} ({why})"
            for _ in range(max(5, int(cfg.live_poll_seconds))):
                if item.stop.is_set():
                    break
                time.sleep(1)

    outdir = Path(item.outdir or cfg.live_dir)
    outdir.mkdir(parents=True, exist_ok=True)
    name = _channel_name(item.url)
    ts_path = outdir / f"{name}_{time.strftime('%Y%m%d_%H%M%S')}.ts"
    item.title = ts_path.stem
    item.path = str(ts_path)
    item.status = "Recording"
    item.started = time.time()
    item.done = 0

    fd = stream.open()
    last_t, last_b = time.time(), 0
    try:
        with open(ts_path, "wb") as f:
            while not item.stop.is_set():
                try:
                    data = fd.read(1 << 16)
                except OSError:
                    break
                if not data:
                    break
                f.write(data)
                item.done += len(data)
                now = time.time()
                if now - last_t >= 1.0:
                    item.speed = (item.done - last_b) / (now - last_t)
                    last_t, last_b = now, item.done
                if item.limit_min and now - item.started >= item.limit_min * 60:
                    break
                if item.limit_mb and item.done >= item.limit_mb * 1048576:
                    break
    finally:
        try:
            fd.close()
        except Exception:
            pass
    if item.done == 0:
        ts_path.unlink(missing_ok=True)
        raise RuntimeError("Stream opened but no data was received")
    _finalize_recording(item, cfg, ts_path)


def _finalize_recording(item: Item, cfg: Config, ts_path: Path) -> None:
    ff = find_ffmpeg()
    if cfg.remux_mp4 and ff and ts_path.exists():
        item.status = "Processing"
        item.speed = 0
        mp4 = ts_path.with_suffix(".mp4")
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        try:
            r = subprocess.run(
                [ff, "-y", "-loglevel", "error", "-i", str(ts_path), "-c", "copy", str(mp4)],
                creationflags=flags, capture_output=True,
            )
            if r.returncode == 0 and mp4.exists() and mp4.stat().st_size > 0:
                ts_path.unlink(missing_ok=True)
                item.path = str(mp4)
        except Exception:
            pass


def run_live_ytdlp(item: Item, cfg: Config) -> None:
    import yt_dlp

    outdir = item.outdir or cfg.live_dir
    Path(outdir).mkdir(parents=True, exist_ok=True)
    opts = _common_ydl_opts(cfg)
    opts["outtmpl"] = str(Path(outdir) / "%(uploader,channel,id)s_%(epoch>%Y%m%d_%H%M%S)s.%(ext)s")
    opts["live_from_start"] = False
    opts["hls_use_mpegts"] = True
    opts.pop("postprocessors", None)
    opts.pop("writesubtitles", None)
    opts["format"] = "best"
    opts["progress_hooks"] = [_make_hook(item)]
    item.started = time.time()
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(item.url, download=True)
        if info:
            item.title = info.get("title") or item.title
            rd = info.get("requested_downloads") or []
            if rd and rd[0].get("filepath"):
                item.path = rd[0]["filepath"]
    except Exception:
        if item.stop.is_set():
            # keep what was recorded so far
            if item._tmp and os.path.exists(item._tmp) and item._final:
                try:
                    os.replace(item._tmp, item._final)
                    item.path = item._final
                except OSError:
                    pass
            return  # treated as a normal (completed) recording
        raise


def run_live(item: Item, cfg: Config) -> None:
    try:
        import streamlink  # noqa: F401
    except ImportError:
        return run_live_ytdlp(item, cfg)
    try:
        run_live_streamlink(item, cfg)
    except _NoPlugin:
        run_live_ytdlp(item, cfg)


_ANSI = __import__("re").compile(r"\x1b\[[0-9;]*[A-Za-z]")  # colour codes yt-dlp puts in error text


_TRANSIENT = re.compile(r"429|too many|rate.?limit|timed? ?out|timeout|temporar|connection (?:reset|aborted|refused|error)|"
                        r"reset by peer|remote end closed|HTTP Error 5\d\d|\b50[0-4]\b|incomplete read|ssl|"
                        r"unable to download (?:webpage|video data)|try again", re.I)
_PERMANENT = re.compile(r"unsupported url|private|removed|deleted|not available|does not exist|404|410|"
                        r"sign in|log in|login|members only|age.?verif|drm|no video found|errno|no such file", re.I)


def _transient(err: str) -> bool:
    """True when the failure looks like a passing problem (busy or rate-limiting site, dropped connection)."""
    return bool(_TRANSIENT.search(err or "")) and not _PERMANENT.search(err or "")


def _worker(item: Item, cfg: Config) -> None:
    try:
        try:
            import yt_dlp.networking._requests  # noqa: F401  (loads, and patches, urllib3 once)
        except Exception:
            pass
        fix_urllib3()
        if item.kind == "download" and cam_detect(item.url):
            item.kind = "live"  # a cam-room link is always a live room: record it
        (run_live if item.kind == "live" else run_download)(item, cfg)
        item.status = "Completed"
        item.eta = None
    except Exception as e:  # noqa: BLE001
        if item.stop.is_set():
            if item.kind == "live" and item.done > 0:
                item.status = "Completed"
            else:
                item.status = "Paused" if item.reason == "pause" else "Stopped"
        else:
            item.status = "Failed"
            item.error = _ANSI.sub("", str(e))[:3000]
            LOG.warning("failed: %s | %s", item.url, item.error.replace("\n", " ")[:400])
            if item.kind == "download" and item.attempts < 2 and _transient(item.error):
                item.attempts += 1
                item.retry_at = time.time() + (120 if item.attempts == 1 else 480)
                item.status = "Retry later"
            low = item.url.lower()
            file_trouble = "Errno" in item.error or "No such file" in item.error
            if file_trouble:
                item.error += ("\n\nHint: Windows couldn't save the file in that folder. Try a folder on your "
                               "main drive (Settings or the Change button next to Save to). Network and cloud "
                               "drives sometimes refuse downloads.")
            if ("x.com/" in low or "twitter.com/" in low) and not item.alt_url and not file_trouble:
                item.error += ("\n\nHint: X shows many videos only to signed-in accounts (sensitive or "
                               "age-restricted posts). In the Browse tab open x.com, sign in once, then add the "
                               "link again. Grabwave reuses that sign-in. Posts that were deleted, or are from a "
                               "private account you don't follow, can't be downloaded.")
            if "beeg." in low and not item.alt_url:
                item.error += ("\n\nHint: This site changed and yt-dlp can't read it from the link alone. "
                               "Open the video in the built-in browser, let it start loading, then click the "
                               "Download button on the page (or the address-bar download button).")
    finally:
        item.speed = 0.0
        item.running = False


# ----------------------------------------------------------------------------
# Manager
# ----------------------------------------------------------------------------

# ----------------------------------------------------------------------------
# Pages with many videos (erome albums / profiles, redgifs profiles): find every video, one item each
# ----------------------------------------------------------------------------
_EROME_RESERVED = {"a", "explore", "search", "user", "upload", "feed", "popular", "new", "hot", "login", "register",
                   "contact", "terms", "privacy", "dmca", "report", "tags", "tag", "faq", "about", "trending",
                   "models", "profile", "settings", "messages", "notifications", "favorites", "following"}
_VIDEO_WORDS = {"video", "videos", "watch", "v", "view", "embed", "clip", "clips", "movie", "movies", "play",
                "player", "gif", "gifs"}
MAX_COLLECTION = 500


def collection_kind(url: str) -> str:
    """Kind of list page this address is ("" = one video): album / profile / user / listing:<reader>."""
    try:
        p = urlparse(url)
    except Exception:  # noqa: BLE001
        return ""
    host = (p.hostname or "").lower()
    parts = [x for x in p.path.split("/") if x]
    if host in ("erome.com", "www.erome.com") and parts:
        if parts[0] == "a" and len(parts) >= 2:
            return "erome-album"
        if len(parts) == 1 and parts[0].lower() not in _EROME_RESERVED:
            return "erome-profile"
    if host in ("redgifs.com", "www.redgifs.com") and len(parts) >= 2 and parts[0] == "users":
        return "redgifs-user"
    if re.search(r"(^|\.)(?:xfree|bodgirls|balbums)\.(?:com|st)$", host) and any(
            x.lower() in ("models", "model", "pornstars", "pornstar", "channels", "channel", "creators", "users",
                          "profile", "profiles", "performers", "girls", "tags", "categories", "category", "album", "albums",
                          "gallery", "galleries", "a", "u")
            for x in parts[:-1] if True) and len(parts) >= 2:
        return "listing:generic"
    if len(parts) == 1 and re.search(r"(^|\.)(?:xfree|bodgirls|balbums)\.(?:com|st)$", host) and parts[0].lower() not in _VIDEO_WORDS \
            and parts[0].lower() not in ("search", "tags", "categories", "login", "signup", "about", "terms", "privacy"):
        return "listing:generic"
    if len(parts) >= 2:
        first = parts[0].lower()
        if re.search(r"(^|\.)pornhub\.(com|org|net)$", host) and first in ("model", "pornstar", "users", "channels",
                                                                          "playlist"):
            return "listing:pornhub"
        if re.search(r"(^|\.)(?:xfree|bodgirls|balbums)\.(?:com|st)$", host) and first not in _VIDEO_WORDS:
            return "listing:generic"
        if re.search(r"(^|\.)redtube\.(com|net)$", host) and first in ("users", "pornstar", "channels", "channel",
                                                                     "playlist", "playlists"):
            return "listing:redtube"
        if re.search(r"(^|\.)xhamster\d*\.[a-z]+$", host) and first in ("users", "creators", "channels", "pornstars"):
            return "listing:xhamster"
        if re.search(r"(^|\.)eporner\.com$", host) and first in ("profile", "pornstar", "channel"):
            return "listing:eporner"
        # any other site: profile / channel / playlist style addresses (yt-dlp's list reader decides if it is one)
        skip = ("youtube.com", "youtu.be", "youtube-nocookie.com", "x.com", "twitter.com", "instagram.com",
                "tiktok.com", "facebook.com", "fb.watch", "google.com")
        if (first in ("users", "user", "u", "model", "models", "pornstar", "pornstars", "channel", "channels", "c",
                      "creator", "creators", "profile", "playlist", "playlists", "members", "member", "actor",
                      "actress", "studio", "studios", "uploader")
                and (len(parts) == 2 or (len(parts) == 3 and parts[2].lower() in ("videos", "uploads", "favorites", "playlists")))
                and not any(host == h or host.endswith("." + h) for h in skip)):
            return "listing:any"
    return ""


def _page_html(url: str, referer: str = "", headers: dict | None = None, timeout: int = 25) -> str:
    hdr = {"User-Agent": browser_ua() or "Mozilla/5.0", "Referer": referer or url, "Accept-Language": "en-US,en;q=0.9"}
    hdr.update(headers or {})
    try:
        from curl_cffi import requests as cr
        r = cr.get(url, headers=hdr, impersonate="chrome", timeout=timeout)
        if r.status_code < 400:
            return r.text
    except Exception:  # noqa: BLE001
        pass
    import ssl
    import urllib.request
    try:
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
    except Exception:  # noqa: BLE001
        ctx = ssl.create_default_context()
    with urllib.request.urlopen(urllib.request.Request(url, headers=hdr), timeout=timeout, context=ctx) as r:
        return r.read(4_000_000).decode("utf-8", "replace")


def _html_unescape(t: str) -> str:
    import html
    return html.unescape(t or "").strip()


def _page_title(h: str) -> str:
    m = re.search(r"<h1[^>]*>(.*?)</h1>", h, re.S | re.I) or re.search(
        r'property=["\']og:title["\'][^>]*content=["\']([^"\']+)', h, re.I)
    if not m:
        return ""
    t = _html_unescape(re.sub(r"<[^>]+>", "", m.group(1)))
    return re.sub(r"\s*\|\s*(EroMe|[A-Za-z0-9 ]+)$", "", t, flags=re.I).strip()


def _page_videos(h: str, url: str) -> list:
    """The direct video files a page plays: <video>/<source> tags (best quality each), else any .mp4 address."""
    out, seen = [], set()
    for vm in re.finditer(r"<video\b([^>]*)>(.*?)</video>", h, re.S | re.I):
        attrs, inner = vm.group(1), vm.group(2)
        poster = re.search(r'poster=["\']([^"\']+)', attrs)
        best, best_res = "", -1
        for sm in re.finditer(r"<source\b([^>]*)>", inner, re.I):
            sa = sm.group(1)
            src = re.search(r'src=["\']([^"\']+)', sa)
            if not src:
                continue
            res = re.search(r'(?:res|label|data-res)=["\'](\d{3,4})', sa)
            r = int(res.group(1)) if res else 0
            if r > best_res:
                best, best_res = src.group(1), r
        if not best:
            src = re.search(r'src=["\']([^"\']+\.(?:mp4|webm|m4v|mov)[^"\']*)', attrs)
            best = src.group(1) if src else ""
        if best:
            best = urljoin(url, _html_unescape(best))
            if best not in seen:
                seen.add(best)
                out.append({"url": best, "referer": url,
                            "thumb": urljoin(url, _html_unescape(poster.group(1))) if poster else ""})
    if not out:  # no <video> tags matched: take any direct video address in the page
        for mm in re.finditer(r'https?://[^"\'\s<>\\]+?\.(?:mp4|webm|m4v|mov)(?:\?[^"\'\s<>\\]*)?', h):
            u2 = _html_unescape(mm.group(0))
            if u2 not in seen:
                seen.add(u2)
                out.append({"url": u2, "referer": url, "thumb": ""})
    return out




def _erome_album(url: str) -> tuple[str, list]:
    """(album title, [entry, ...]) for one album page, own page reader."""
    h = _page_html(url, "https://www.erome.com/")
    return _page_title(h), _page_videos(h, url)


def _erome_profile_albums(url: str) -> tuple[str, list]:
    p = urlparse(url)
    name = [x for x in p.path.split("/") if x][0]
    albums, seen = [], set()
    for page in range(1, 31):
        sep = "&" if "?" in url else "?"
        h = _page_html(url if page == 1 else f"{url}{sep}page={page}", "https://www.erome.com/")
        found = [m.group(1) for m in re.finditer(r'href=["\'](?:https?://(?:www\.)?erome\.com)?/a/([A-Za-z0-9_-]+)', h)]
        new = [a for a in dict.fromkeys(found) if a not in seen]
        if not new:
            break
        seen.update(new)
        albums += new
        if len(albums) >= 120:
            break
    return name, [f"https://www.erome.com/a/{a}" for a in albums]


def _erome_entries(url: str, cfg: Config) -> tuple[str, list]:
    kind = collection_kind(url)
    if kind == "erome-album":
        title, ents = _erome_album(url)
        return title, ents
    name, albums = _erome_profile_albums(url)
    ents = []
    for a in albums:
        try:
            t, es = _erome_album(a)
        except Exception:  # noqa: BLE001
            continue
        for i, e in enumerate(es, 1):
            e["filename"] = _safe_name(f"{t or a.rsplit('/', 1)[-1]} {i:02d}" if len(es) > 1 else (t or a.rsplit('/', 1)[-1]))
        ents += es
        if len(ents) >= MAX_COLLECTION:
            break
    return name, ents


def _redgifs_api(url: str, cfg: Config) -> tuple[str, list]:
    import json as _json
    name = [x for x in urlparse(url).path.split("/") if x][1]
    ua = browser_ua() or "Mozilla/5.0"
    tok = _json.loads(_page_html("https://api.redgifs.com/v2/auth/temporary", "https://www.redgifs.com/",
                                 {"User-Agent": ua}))["token"]
    hdr = {"Authorization": f"Bearer {tok}", "User-Agent": ua, "Origin": "https://www.redgifs.com"}
    ents, page, pages = [], 1, 1
    while page <= pages and len(ents) < MAX_COLLECTION:
        d = _json.loads(_page_html(
            f"https://api.redgifs.com/v2/users/{name}/search?order=recent&type=g&page={page}",
            "https://www.redgifs.com/", hdr))
        pages = int(d.get("pages") or 1)
        for g in d.get("gifs") or []:
            gid = g.get("id")
            if not gid:
                continue
            u = g.get("urls") or {}
            ents.append({"url": f"https://www.redgifs.com/watch/{gid}", "referer": "https://www.redgifs.com/",
                         "filename": _safe_name(f"{name} {gid}"), "thumb": u.get("poster") or u.get("thumbnail") or "",
                         "alt_url": u.get("hd") or u.get("sd") or "", "alt_referer": "https://www.redgifs.com/"})
        page += 1
    return name, ents


def _ytdlp_flat(url: str, cfg: Config) -> tuple[str, list]:
    import yt_dlp
    opts = _common_ydl_opts(cfg, True, "")
    opts.update({"extract_flat": "in_playlist", "skip_download": True, "noplaylist": False, "playlistend": MAX_COLLECTION})
    opts.pop("postprocessors", None)
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(_normalize_url(url), download=False)
    ents = []
    for e in (info or {}).get("entries") or []:
        if not e:
            continue
        u = e.get("url") or e.get("webpage_url") or ""
        if u.lower().startswith(("http://", "https://")):
            ents.append({"url": u, "referer": url, "thumb": e.get("thumbnail") or "",
                         "filename": _safe_name(e.get("title") or "") if e.get("title") else ""})
    return (info or {}).get("title") or "", ents


def _gdl_entries(url: str, cfg: Config) -> tuple[str, list]:
    from gallery_dl import config as gconf, extractor as gext
    gconf.clear()
    cf = effective_cookie_file(cfg)
    if cf:
        gconf.set(("extractor",), "cookies", cf)
    ex = gext.find(url)
    if ex is None:
        return "", []
    ents = []
    for msg in ex:
        if msg and msg[0] == 3 and len(msg) >= 3:  # (Message.Url, url, kwdict)
            u, kw = msg[1], msg[2] or {}
            ext = str(kw.get("extension") or u.rsplit(".", 1)[-1]).lower().split("?")[0]
            if ext in ("mp4", "webm", "m4v", "mov", "mkv"):
                ents.append({"url": u, "referer": url, "thumb": "",
                             "filename": _safe_name(str(kw.get("filename") or ""))})
                if len(ents) >= MAX_COLLECTION:
                    break
    return "", ents


def _listing_pages(url: str, site: str):
    """Yield the address of page 1, 2, 3 ... of a profile / channel video list."""
    p = urlparse(url)
    path = p.path.rstrip("/")
    low = path.lower()
    base = f"{p.scheme}://{p.netloc}"
    if site == "redtube":
        for n in range(1, 40):
            yield f"{base}{path}" + (f"?page={n}" if n > 1 else "")
    elif site == "pornhub":
        if not low.endswith("/videos") and "/playlist/" not in low and "/videos/" not in low:
            path += "/videos"
        for n in range(1, 40):
            yield f"{base}{path}" + (f"?page={n}" if n > 1 else "")
    elif site == "xhamster":
        path = re.sub(r"/\d+$", "", path)
        if ("/users/" in low or "/creators/" in low) and not re.search(r"/(videos|best|newest)$", low):
            path += "/videos"
        for n in range(1, 40):
            yield f"{base}{path}" + (f"/{n}" if n > 1 else "")
    else:  # eporner
        path = re.sub(r"/\d+$", "", path)
        for n in range(1, 40):
            yield f"{base}{path}/" + (f"{n}/" if n > 1 else "")


_LISTING_LINK = {
    "pornhub": r'href=["\'](/view_video\.php\?viewkey=[A-Za-z0-9]+)',
    "xhamster": r'href=["\'](https?://[^"\']*xhamster\d*\.[a-z]+/videos/[^"\'#?]+|/videos/[^"\'#?]+)',
    "redtube": r'href=["\'](?:https?://(?:www\.)?redtube\.(?:com|net))?(/\d{4,9})(?=["\'?#/])',
    "eporner": r'href=["\'](/(?:video-[A-Za-z0-9]+|hd-porn/[A-Za-z0-9]+)/[^"\'#?]*)',
}


def _listing_scrape(url: str, cfg: Config) -> tuple[str, list]:
    """Own page reader for profile / channel pages: collects the video links page by page."""
    site = collection_kind(url).split(":", 1)[1]
    if site not in _LISTING_LINK:
        return _generic_listing(url, cfg)
    pat = re.compile(_LISTING_LINK[site], re.I)
    ents, seen = [], set()
    for page_url in _listing_pages(url, site):
        try:
            h = _page_html(page_url, url)
        except Exception:  # noqa: BLE001
            break
        new = 0
        for m in pat.finditer(h):
            link = urljoin(page_url, _html_unescape(m.group(1)))
            key = re.sub(r"[?#].*$", "", link) if site != "pornhub" else link
            if key in seen:
                continue
            seen.add(key)
            new += 1
            ents.append({"url": link, "referer": url, "thumb": "", "filename": ""})
        if not new or len(ents) >= MAX_COLLECTION:
            break
    return "", ents


def _generic_links(html: str, page_url: str) -> list:
    """Layout-independent: the video links of a profile page are the repeated thumbnail links that share one
    address pattern. Returns [(link, thumb, title), ...] for the biggest such group, or [] (also for a page that is
    itself one video)."""
    from collections import Counter
    if re.search(r'property=["\']og:type["\'][^>]*content=["\']video', html, re.I) or \
            re.search(r'content=["\']video[^"\']*["\'][^>]*property=["\']og:type', html, re.I) or \
            '"@type":"VideoObject"' in html.replace(" ", "") or "application/ld+json" in html and "VideoObject" in html:
        return []
    host = urlparse(page_url).netloc.lower()
    here = urlparse(page_url).path.rstrip("/")
    cands = []
    for m in re.finditer(r"<a\b([^>]*?)href=[\"']([^\"'#]+)[\"']([^>]*)>(.*?)</a>", html, re.S | re.I):
        inner = m.group(4)
        if "<img" not in inner.lower() and "data-src" not in inner.lower() and "poster" not in inner.lower():
            continue
        link = urljoin(page_url, _html_unescape(m.group(2)))
        pu = urlparse(link)
        if pu.netloc.lower().removeprefix("www.") != host.removeprefix("www.") or pu.path.rstrip("/") in ("", here):
            continue
        segs = [x for x in pu.path.split("/") if x]
        tmpl = (segs[0].lower() if segs else "", len(segs))
        img = re.search(r'(?:data-src|data-original|src)=["\']([^"\']+)', inner, re.I)
        ttl = re.search(r'(?:title|alt)=["\']([^"\']{3,})', m.group(1) + m.group(3) + inner, re.I)
        cands.append((tmpl, link, urljoin(page_url, _html_unescape(img.group(1))) if img else "",
                      _html_unescape(ttl.group(1)) if ttl else ""))
    if not cands:
        return []
    common, n = Counter(t for t, *_ in cands).most_common(1)[0]
    if n < 3 or common[0] in ("tag", "tags", "category", "categories", "user", "users", "profile", "channel",
                              "channels", "model", "models", "pornstar", "pornstars", "creator", "creators"):
        return []
    seen, out = set(), []
    for t, link, th, ti in cands:
        if t == common and link not in seen:
            seen.add(link)
            out.append((link, th, ti))
    return out


def _next_link(html: str, page_url: str) -> str:
    """The address of the next page of a list, from rel="next", a "Next" link or a » / › arrow; '' if none."""
    m = (re.search(r'<(?:a|link)\b[^>]*\brel=["\'][^"\']*\bnext\b[^"\']*["\'][^>]*\bhref=["\']([^"\']+)', html, re.I)
         or re.search(r'<(?:a|link)\b[^>]*\bhref=["\']([^"\']+)["\'][^>]*\brel=["\'][^"\']*\bnext\b', html, re.I))
    if not m:
        for a in re.finditer(r'<a\b[^>]*\bhref=["\']([^"\'#]+)["\'][^>]*>(.*?)</a>', html, re.S | re.I):
            label = re.sub(r"<[^>]+>|&[a-z#0-9]+;|\s+", " ", a.group(2)).strip().lower()
            cls = a.group(0)[:200].lower()
            if label in ("next", "next page", "next »", "›", "»", ">", "older", "next >") or "class=\"next" in cls \
                    or "pagination-next" in cls or "page-next" in cls:
                m = a
                break
    if not m:
        return ""
    nxt = urljoin(page_url, _html_unescape(m.group(1)))
    return "" if nxt.split("#")[0].rstrip("/") == page_url.split("#")[0].rstrip("/") or nxt.lower().startswith("javascript") else nxt


_DEEP_HOSTS = ("balbums.st", "balbums.com")


def _generic_listing(url: str, cfg: Config) -> tuple[str, list]:
    """Profile / model / album pages on sites without their own reader.
    1. A page that plays videos itself (an album) -> those videos.
    2. Otherwise the repeated thumbnail links over all pages (follows "Next", or tries page-number styles);
       on album-style sites each linked page is opened and its videos are used."""
    host = (urlparse(url).hostname or "").lower()
    deep_site = any(host == h or host.endswith("." + h) for h in _DEEP_HOSTS)
    if deep_site:
        try:
            first = _page_html(url, url)
        except Exception:  # noqa: BLE001
            first = ""
        vids = _page_videos(first, url) if first else []
        if vids:
            title = _page_title(first)
            for n, e in enumerate(vids, 1):
                e["filename"] = _safe_name(f"{title} {n:02d}" if title and len(vids) > 1 else title)
            return title, vids
    title, ents = _generic_pages(url, cfg)
    if ents and deep_site:
        deep, tried = [], 0
        for e in ents:
            tried += 1
            try:
                h = _page_html(e["url"], url)
                vids = _page_videos(h, e["url"])
                t2 = _page_title(h)
            except Exception:  # noqa: BLE001
                vids, t2 = [], ""
            if vids:
                for n, v in enumerate(vids, 1):
                    v["filename"] = _safe_name(f"{t2} {n:02d}" if t2 and len(vids) > 1 else t2)
                deep += vids
            else:
                deep.append(e)
            if len(deep) >= MAX_COLLECTION or tried >= 150:
                break
        return title, deep
    return title, ents


def _generic_pages(url: str, cfg: Config) -> tuple[str, list]:
    """All pages of a profile / model page: follow the page's own "next" link; if it has none, try the common
    page-number styles (?page=2, /page/2/, /2/, ?p=2) on page 2 and keep the first that brings new links."""
    base = url.split("#")[0]
    clean = re.sub(r"[?&](?:page|p)=\d+", "", base)
    path_only = clean.split("?")[0].rstrip("/")
    query = ("?" + clean.split("?", 1)[1]) if "?" in clean else ""
    sep = "&" if "?" in clean else "?"
    styles = [lambda n: f"{clean}{sep}page={n}", lambda n: f"{path_only}/page/{n}/{query}",
              lambda n: f"{path_only}/{n}/{query}", lambda n: f"{clean}{sep}p={n}"]
    ents, seen, visited = [], set(), set()

    def grab(page_url):
        """-> (new links on that page, address of its next page)"""
        visited.add(page_url)
        try:
            html = _page_html(page_url, url)
        except Exception:  # noqa: BLE001
            return 0, ""
        new = 0
        for link, th, ti in _generic_links(html, page_url):
            if link in seen:
                continue
            seen.add(link)
            new += 1
            ents.append({"url": link, "referer": url, "thumb": th, "filename": ""})
        return new, _next_link(html, page_url)

    new, nxt = grab(base)
    if not new:
        return "", ents
    if nxt:  # the page tells us where the next one is
        for _ in range(80):
            if not nxt or nxt in visited or len(ents) >= MAX_COLLECTION:
                break
            new, nxt = grab(nxt)
            if not new:
                break
        if len(ents) > 0 and not (nxt and nxt not in visited):
            return "", ents
    style = None
    for st in styles:  # no usable "next" link: find the page-number style that works
        new, _n = grab(st(2))
        if new:
            style = st
            break
    if style is not None:
        for n in range(3, 80):
            if len(ents) >= MAX_COLLECTION or not grab(style(n))[0]:
                break
    return "", ents


def _safe_name(t: str) -> str:
    return re.sub(r'[\\/:*?"<>|%\r\n\t]', "", t or "").strip()[:140].strip(" .")


def expand_collection(url: str, cfg: Config) -> tuple[str, list]:
    """Every video on an album / profile / channel page -> (group title, [entry dict, ...]).
    Tries Grabwave's own page reader first, then yt-dlp, then gallery-dl. Empty list = not a collection."""
    kind = collection_kind(url)
    if not kind:
        return "", []
    routes = {"erome-album": [_erome_entries, _ytdlp_flat, _gdl_entries],
              "erome-profile": [_erome_entries, _ytdlp_flat, _gdl_entries],
              "redgifs-user": [_redgifs_api, _ytdlp_flat, _gdl_entries]}.get(
        kind, [_ytdlp_flat, _listing_scrape, _gdl_entries])
    for fn in routes:
        try:
            title, ents = fn(url, cfg)
        except Exception as ex:  # noqa: BLE001
            LOG.info("page reader %s failed for %s: %s", getattr(fn, "__name__", fn), url, str(ex)[:200])
            continue
        seen, uniq = set(), []
        for e in ents:
            if e["url"] not in seen:
                seen.add(e["url"])
                uniq.append(e)
        if uniq:
            if not title:
                segs = [x for x in urlparse(url).path.split("/") if x and not x.isdigit()
                        and x.lower() not in ("videos", "profile", "users", "model", "pornstar", "channel", "channels",
                                              "creators", "pornstars", "playlist")]
                title = segs[-1].replace("-", " ").replace("_", " ") if segs else ""
            return title, uniq[:MAX_COLLECTION]
    return "", []



class Manager:
    def __init__(self, cfg: Config):
        _init_log()
        self.cfg = cfg
        self.items: list[Item] = []
        self.notice = ""  # one-line message for the status bar (e.g. "Found 12 videos")
        self.confirm = None     # optional callback(group, count) -> how many to keep (0 = cancel); set by the window
        self._asking = False
        self._host_last: dict = {}
        self._free_checked = 0.0
        self._free_mb = 10 ** 9
        self._saved_at = time.time()
        self._low_said = 0.0
        self._threads: dict[str, threading.Thread] = {}
        self.load()
        for old in config_dir().glob("cj_*.txt"):  # leftover private cookie copies
            try:
                if time.time() - old.stat().st_mtime > 86400:
                    old.unlink()
            except OSError:
                pass

    # --- queue ------------------------------------------------------------
    @staticmethod
    def _key(url: str) -> str:
        u = _normalize_url((url or "").strip()).split("#")[0].rstrip("/").lower()
        return re.sub(r"^https?://(www\.)?", "", u)

    def _existing(self, url: str, skip=None):
        k = self._key(url)
        for i in self.items:
            if i is skip or self._key(i.url) != k:
                continue
            if i.status in ("Failed", "Stopped"):
                continue
            if i.status == "Completed" and i.path and not os.path.exists(i.path):
                continue  # the saved file is gone: allow downloading it again
            return i
        return None

    def add(self, url: str, kind: str = "download", outdir: str = "", html: str = "") -> Item:
        dup = self._existing(url) if kind == "download" else None
        if dup is not None:
            self.notice = "That link is already in your list."
            return dup
        it = Item(url.strip(), kind, outdir)
        it._dom_html = html  # the page as the Browse tab shows it (everything loaded by scrolling)
        self.items.append(it)
        if kind == "download" and collection_kind(it.url):  # album / profile: find every video first
            it.status = "Checking"
            it.title = "Looking for videos…"
            threading.Thread(target=self._expand_worker, args=(it,), daemon=True).start()
        return it

    def _expand_worker(self, it: Item) -> None:
        dom = []
        if it._dom_html:
            try:
                dom = [{"url": l, "referer": it.url, "thumb": th, "filename": ""}
                       for l, th, _t in _generic_links(it._dom_html, it.url)]
            except Exception:  # noqa: BLE001
                dom = []
        try:
            title, ents = expand_collection(it.url, self.cfg)
        except Exception as ex:  # noqa: BLE001
            LOG.warning("expand failed for %s: %s", it.url, str(ex)[:200])
            title, ents = "", []
        have = {e["url"] for e in dom}
        it._dom_html = ""
        it._pending = (title, dom + [e for e in ents if e["url"] not in have])

    def _apply_expansion(self, it: Item) -> None:
        title, ents = it._pending
        it._pending = None
        if it not in self.items:  # removed while looking
            return
        if not ents:
            it.status, it.title = "Queued", it.url  # fall back to the normal single-page route
            self.notice = "No separate videos found; trying the page as one video."
            return
        group = _safe_name(title)
        outdir = it.outdir
        if not outdir and len(ents) > 1 and group:
            outdir = str(Path(self.cfg.download_dir) / group)
        total = len(ents)
        kept = []
        for e in ents:  # skip what is already in the list or already saved
            if self._existing(e["url"], skip=it) is not None:
                continue
            fn = e.get("filename", "")
            if fn and outdir:
                import glob
                if glob.glob(os.path.join(glob.escape(outdir), glob.escape(fn) + ".*")):
                    continue
            kept.append(e)
        skipped = total - len(kept)
        if not kept:
            self.items.remove(it)
            self.notice = f"Nothing new: all {total} videos are already in your list or saved."
            self.save()
            return
        limit = int(getattr(self.cfg, "confirm_batch_over", 25) or 0)
        if limit and len(kept) > limit and callable(self.confirm):
            self._asking = True
            try:
                n = int(self.confirm(group, len(kept)) or 0)
            except Exception:  # noqa: BLE001
                n = len(kept)
            finally:
                self._asking = False
            if n <= 0:
                self.items.remove(it)
                self.notice = "Cancelled. Nothing was added."
                self.save()
                return
            kept = kept[:n]
        base = list(self.items).index(it)
        extra = []
        for n, e in enumerate(kept):
            tgt = it if n == 0 else Item(e["url"], "download", outdir)
            tgt.url, tgt.outdir = e["url"], outdir
            tgt.referer = e.get("referer", "") if e["url"] != e.get("referer") else ""
            tgt.filename = e.get("filename", "")
            tgt.title = tgt.filename or e["url"]
            tgt.thumb = e.get("thumb", "")
            tgt.alt_url, tgt.alt_referer = e.get("alt_url", ""), e.get("alt_referer", "")
            if not tgt.alt_url and re.search(r"\.(mp4|webm|m4v|mov)(\?|$)", e["url"], re.I):
                tgt.alt_url, tgt.alt_referer = e["url"], e.get("referer", "")  # second route: ffmpeg copy
            tgt.quality = it.quality
            tgt.status = "Queued"
            if n:
                extra.append(tgt)
        self.items[base + 1:base + 1] = extra
        self.notice = f"Found {total} video{'s' if total != 1 else ''}" + (f" in {group}" if group else "") + \
            (f"; {len(kept)} added, {skipped} already there" if skipped or len(kept) != total else "") + "."
        LOG.info("expanded %s -> %d (kept %d)", it.url, total, len(kept))
        self.save()

    def active_count(self) -> int:
        return sum(1 for i in self.items if i.running)

    HOST_LIMIT = 3       # most downloads at once from one site
    HOST_GAP = 1.2       # seconds between starting two downloads from one site
    MIN_FREE_MB = 1024   # do not start new downloads below this much free space
    STOP_FREE_MB = 250   # stop recordings below this much free space

    @staticmethod
    def _site(url: str) -> str:
        h = (urlparse(url).hostname or "").lower()
        return ".".join(h.split(".")[-2:]) if h else ""

    def _free(self) -> int:
        now = time.time()
        if now - self._free_checked > 10:
            self._free_checked = now
            p = Path(self.cfg.download_dir)
            while not p.exists() and p.parent != p:
                p = p.parent
            try:
                self._free_mb = shutil.disk_usage(str(p)).free // (1024 * 1024)
            except Exception:  # noqa: BLE001
                self._free_mb = 10 ** 9
        return self._free_mb

    def tick(self) -> None:
        if self._asking:  # a question is open on screen
            return
        now = time.time()
        for it in [i for i in self.items if i._pending is not None]:
            self._apply_expansion(it)
        for it in self.items:  # automatic second tries
            if it.status == "Retry later" and now >= it.retry_at:
                it.status = "Queued"
        free = self._free()
        if free < self.STOP_FREE_MB:
            for it in self.items:
                if it.running and it.kind == "live":
                    self.request_stop(it, "stop")
        low = free < self.MIN_FREE_MB
        if low and now - self._low_said > 60:
            self._low_said = now
            self.notice = f"Low disk space ({free} MB free). New downloads wait until there is more room."
        slots = effective_slots(self.cfg) - self.active_count()
        per_site: dict = {}
        for i in self.items:
            if i.running and i.kind == "download":
                k = self._site(i.url)
                per_site[k] = per_site.get(k, 0) + 1
        for it in self.items:
            if slots <= 0:
                break
            if it.status != "Queued" or it.running:
                continue
            if low and it.kind == "download":
                continue
            if it.kind == "download":
                k = self._site(it.url)
                if per_site.get(k, 0) >= self.HOST_LIMIT or now - self._host_last.get(k, 0) < self.HOST_GAP:
                    continue
                per_site[k] = per_site.get(k, 0) + 1
                self._host_last[k] = now
            self._start(it)
            slots -= 1
        if now - self._saved_at > 30:  # keep the list safe if the computer loses power
            self._saved_at = now
            self.save()

    def _start(self, it: Item) -> None:
        it.stop = threading.Event()
        it.reason = ""
        it.error = ""
        it.running = True
        it.status = "Recording" if it.kind == "live" else "Downloading"
        t = threading.Thread(target=_worker, args=(it, self.cfg), daemon=True)
        self._threads[it.id] = t
        t.start()

    def request_stop(self, it: Item, reason: str = "stop") -> None:
        if it.kind == "live" and reason == "pause":
            reason = "stop"  # live streams can't be paused
        if it.running:
            it.reason = reason
            it.stop.set()
        elif it.status == "Queued":
            it.status = "Paused" if reason == "pause" else "Stopped"

    def resume(self, it: Item) -> None:
        if not it.running and it.status in ("Paused", "Stopped", "Failed"):
            it.status = "Queued"
            it.error = ""

    def restart(self, it: Item) -> None:
        if it.running:
            return
        it.done = it.total = 0
        it.status = "Queued"
        it.error = ""

    def remove(self, it: Item, delete_file: bool = False) -> None:
        self.request_stop(it, "stop")
        t = self._threads.pop(it.id, None)
        if t:
            t.join(timeout=3)
        if delete_file and it.path and os.path.exists(it.path):
            try:
                os.remove(it.path)
            except OSError:
                pass
        if it in self.items:
            self.items.remove(it)

    def shutdown(self) -> None:
        for it in self.items:
            self.request_stop(it, "pause")
        for t in list(self._threads.values()):
            t.join(timeout=8)
        self.save()

    # --- persistence ------------------------------------------------------
    @staticmethod
    def _queue_path() -> Path:
        return config_dir() / "queue.json"

    def save(self) -> None:
        try:
            self._queue_path().write_text(
                json.dumps([i.to_json() for i in self.items], indent=1), "utf-8")
        except Exception:
            pass

    def load(self) -> None:
        try:
            data = json.loads(self._queue_path().read_text("utf-8"))
            self.items = [Item.from_json(d) for d in data]
        except Exception:
            self.items = []


# ----------------------------------------------------------------------------
# Thumbnails, link preview, convert / trim
# ----------------------------------------------------------------------------

def thumb_dir() -> Path:
    d = config_dir() / "thumbs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def thumb_path(item: Item) -> Path:
    return thumb_dir() / f"{item.id}.jpg"


def fetch_thumb(item: Item) -> str:
    """Make sure a small preview picture exists for the item; returns its path or ''."""
    out = thumb_path(item)
    if out.exists() and out.stat().st_size > 0:
        return str(out)
    try:
        if item.path and os.path.isfile(item.path):
            ff = find_ffmpeg()
            if ff and Path(item.path).suffix.lower() not in (".mp3", ".m4a", ".opus", ".ogg", ".wav", ".flac"):
                for ss in ("3", "0"):
                    r = subprocess.run([ff, "-y", "-loglevel", "error", "-ss", ss, "-i", item.path,
                                        "-frames:v", "1", "-vf", "scale=360:-2", str(out)],
                                       capture_output=True, timeout=40)
                    if r.returncode == 0 and out.exists() and out.stat().st_size > 0:
                        return str(out)
        if item.thumb.lower().startswith(("http://", "https://")):
            import urllib.request
            req = urllib.request.Request(item.thumb, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=15) as r:
                data = r.read(4_000_000)
            if data:
                out.write_bytes(data)
                return str(out)
    except Exception:
        pass
    return ""


def probe(url: str, cfg: Config) -> dict:
    """Quick look at a link (title, length, picture, size) without downloading it."""
    if cam_detect(url):
        return {"live": True, "title": cam_username(url) or url}
    try:
        import yt_dlp.networking._requests  # noqa: F401
    except Exception:
        pass
    fix_urllib3()
    import yt_dlp
    opts = _common_ydl_opts(cfg)
    opts.update({"skip_download": True, "socket_timeout": 15, "retries": 1, "extractor_retries": 1})
    for k in ("postprocessors", "writesubtitles", "subtitleslangs", "merge_output_format", "postprocessor_args"):
        opts.pop(k, None)
    opts["format"] = "best"
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    if not info:
        return {}
    if info.get("_type") == "playlist" and info.get("entries"):
        info = next((x for x in info["entries"] if x), info)
    heights = sorted({f.get("height") for f in (info.get("formats") or []) if f.get("height")}, reverse=True)
    size = info.get("filesize") or info.get("filesize_approx") or 0
    return {"title": info.get("title") or url, "duration": int(info.get("duration") or 0),
            "thumbnail": info.get("thumbnail") or "", "size": int(size), "heights": heights}


def _ffmpeg_run(args: list[str]) -> None:
    ff = find_ffmpeg()
    if not ff:
        raise RuntimeError("ffmpeg was not found (reinstall Grabwave)")
    r = subprocess.run([ff, "-y", "-loglevel", "error"] + args, capture_output=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.decode("utf-8", "replace")[-600:] or "ffmpeg failed")


def convert_file(path: str, kind: str, cfg: Config) -> str:
    """Convert a finished file to 'mp3' or 'mp4' next to the original. Returns the new path."""
    src = Path(path)
    out = src.with_name(src.stem + f" (converted).{kind}")
    th = ["-threads", str(perf(cfg)["threads"])] if perf(cfg)["threads"] else []
    if kind == "mp3":
        _ffmpeg_run(["-i", str(src), "-vn", "-codec:a", "libmp3lame", "-q:a", "2"] + th + [str(out)])
    else:
        _ffmpeg_run(["-i", str(src), "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                     "-c:a", "aac", "-movflags", "+faststart"] + th + [str(out)])
    return str(out)


def trim_file(path: str, start: str, end: str) -> str:
    """Cut a piece out of a file (times like 90, 1:30 or 0:01:30) without re-encoding."""
    src = Path(path)
    out = src.with_name(src.stem + f" (trimmed){src.suffix}")
    args = []
    if start:
        args += ["-ss", start]
    if end:
        args += ["-to", end]
    _ffmpeg_run(args + ["-i", str(src), "-c", "copy", str(out)])
    return str(out)


# ----------------------------------------------------------------------------
# Formatting helpers
# ----------------------------------------------------------------------------

def fmt_size(n: float) -> str:
    if not n:
        return "—"
    n = float(n)
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or u == "TB":
            return f"{n:.0f} {u}" if u == "B" else f"{n:.1f} {u}"
        n /= 1024
    return ""


def fmt_speed(n: float) -> str:
    return f"{fmt_size(n)}/s" if n else ""


def fmt_dur(sec: float | None) -> str:
    if sec is None:
        return ""
    sec = int(sec)
    h, r = divmod(sec, 3600)
    m, s = divmod(r, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


# ============================================================================================
# Watch: play a page's video inside Grabwave even where the built-in browser can't (H.264)
#   resolve_stream() asks yt-dlp for the direct video address(es) without downloading,
#   StreamRelay hands them to ffmpeg and serves the result on a private local address,
#   and the player window (Qt's own media player, which can decode H.264) plays that address.
# ============================================================================================
def _watch_headers(url: str, extra: dict | None = None) -> dict:
    h = {"Referer": url, "User-Agent": browser_ua() or CAM_UA}
    h.update(extra or {})
    return h


def _cam_watch_input(url: str, cfg: Config, alt_url: str, alt_referer: str):
    """Live room: returns (playlist address, headers) or raises."""
    if alt_url and ".m3u8" in alt_url.lower():  # the address the built-in browser saw is the surest route
        return alt_url, _watch_headers(alt_referer or url)
    sl = _new_session(cfg)
    stream = cam_open_stream(sl, url, cfg.live_quality, cfg.cookies_browser, effective_cookie_file(cfg))
    addr = getattr(stream, "url", "") or ""
    if not addr:
        raise RuntimeError("no playlist address")
    return addr, _watch_headers(url)


def resolve_stream(url: str, cfg: Config, alt_url: str = "", alt_referer: str = "") -> dict:
    """Returns {"inputs": [address, ...], "headers": {...}, "title": str} for the video on a page.
    Routes, in order: live room resolver -> yt-dlp (with cookies) -> yt-dlp (other YouTube clients, no cookies)
    -> KVS page reader -> address the built-in browser saw."""
    errors = []
    if url.lower().startswith(("http://", "https://")):
        if cam_detect(url):
            try:
                addr, hdrs = _cam_watch_input(url, cfg, alt_url, alt_referer)
                return {"inputs": [addr], "headers": hdrs, "title": _channel_name(url)}
            except CamOffline as ex:
                raise RuntimeError(f"This room isn't live right now ({ex}).")
            except Exception as ex:  # noqa: BLE001
                errors.append(_ANSI.sub("", str(ex))[:200])
        try:
            import yt_dlp
        except Exception as ex:  # noqa: BLE001
            yt_dlp = None
            errors.append(str(ex)[:200])
        if yt_dlp is not None:
            low = url.lower()
            attempts = [("with your sign-in", None, True)]
            if any(h in low for h in ("youtube.com", "youtu.be")):
                attempts += [("other YouTube route 1", {"youtube": {"player_client": ["ios", "web_safari"]}}, False),
                             ("other YouTube route 2", {"youtube": {"player_client": ["tv", "android_vr"]}}, False)]
            else:
                attempts += [("without sign-in", None, False)]
            for label, extractor_args, use_cookies in attempts:
                try:
                    opts = _common_ydl_opts(cfg, True, "")
                    opts["format"] = "b[height<=1080]/bv*[height<=1080]+ba/b/bv*+ba/best"
                    opts["skip_download"] = True
                    opts.pop("postprocessors", None)
                    if not use_cookies:
                        for k in ("cookiefile", "cookiesfrombrowser"):
                            opts.pop(k, None)
                    if extractor_args:
                        opts["extractor_args"] = extractor_args
                    with yt_dlp.YoutubeDL(opts) as ydl:
                        info = ydl.extract_info(_normalize_url(url), download=False)
                        if info and info.get("entries"):
                            info = next((x for x in info["entries"] if x), info)
                        fmts = info.get("requested_formats") or [info]
                        inputs = [f["url"] for f in fmts if f.get("url")]
                        if inputs:
                            headers = dict(info.get("http_headers") or {})
                            for f in fmts:
                                headers.update(f.get("http_headers") or {})
                            try:
                                ck = ydl.cookiejar.get_cookie_header(inputs[0])
                                if ck:
                                    headers["Cookie"] = ck
                            except Exception:  # noqa: BLE001
                                pass
                            return {"inputs": inputs[:2], "headers": headers, "title": info.get("title") or ""}
                except Exception as ex:  # noqa: BLE001
                    errors.append(_ANSI.sub("", str(ex))[:200])
        try:  # sites built on the KVS script (the page lists its own video addresses)
            cands = kvs_candidates(url)
            if cands:
                return {"inputs": [cands[0]], "headers": {"Referer": url, "User-Agent": browser_ua() or "Mozilla/5.0"},
                        "title": ""}
        except Exception:  # noqa: BLE001
            pass
    if alt_url:  # a stream address the built-in browser saw (in its best quality)
        return {"inputs": [upgrade_stream(alt_url, alt_referer or url, 1080)],
                "headers": _watch_headers(alt_referer or url), "title": ""}
    raise RuntimeError(errors[0] if errors else "Couldn't find a video on this page.")


class StreamRelay:
    """Serves the video from `inputs` as one stream on http://127.0.0.1:<port>/video.mkv.
    ffmpeg copies the picture untouched and only re-encodes the sound (cheap), so playback is smooth."""

    def __init__(self, inputs: list, headers: dict, cfg: Config):
        self.inputs, self.headers, self.cfg = list(inputs), dict(headers), cfg
        self.server = None
        self.procs: list = []
        self._lock = threading.Lock()

    def _command(self) -> list:
        ff = find_ffmpeg()
        if not ff:
            raise RuntimeError("ffmpeg not found")
        hdr = "".join(f"{k}: {v}\r\n" for k, v in self.headers.items() if k.lower() != "accept-encoding")
        cmd = [ff, "-loglevel", "error", "-nostdin"]
        if self.cfg.proxy:
            cmd += ["-http_proxy", self.cfg.proxy]
        for u in self.inputs:
            cmd += ["-headers", hdr, "-i", u]
        if len(self.inputs) == 2:
            cmd += ["-map", "0:v:0", "-map", "1:a:0"]
        else:
            cmd += ["-map", "0:v:0?", "-map", "0:a:0?"]
        return cmd + ["-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-f", "matroska", "pipe:1"]

    def start(self) -> str:
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        relay = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *a):  # keep quiet
                pass

            def _head(self):
                self.send_response(200)
                self.send_header("Content-Type", "video/x-matroska")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()

            def do_HEAD(self):
                self._head()

            def do_GET(self):
                try:
                    proc = subprocess.Popen(relay._command(), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
                except Exception:  # noqa: BLE001
                    self.send_error(500)
                    return
                with relay._lock:
                    relay.procs.append(proc)
                try:
                    self._head()
                    while True:
                        chunk = proc.stdout.read(65536)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionError, OSError):
                    pass
                finally:
                    try:
                        proc.kill()
                    except Exception:  # noqa: BLE001
                        pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{self.server.server_port}/video.mkv"

    def stop(self) -> None:
        with self._lock:
            for p in self.procs:
                try:
                    p.kill()
                except Exception:  # noqa: BLE001
                    pass
            self.procs.clear()
        if self.server is not None:
            try:
                self.server.shutdown()
                self.server.server_close()
            except Exception:  # noqa: BLE001
                pass
            self.server = None


# ============================================================================================
# Live-cam sites (public rooms only)
# Direct stream resolvers for a few live-cam sites (public rooms only).
#
# yt-dlp's extractors for these sites break often, so the recorder asks the site's
# own web API for the HLS playlist and hands it to streamlink's HLS reader.
#
# Requests are made with a Chrome-like TLS fingerprint (curl_cffi, if installed) and,
# optionally, the cookies from the user's own browser, so the site sees the same
# session the user already has. Nothing here solves challenges or decodes protected
# streams: if the site refuses, the error is reported as-is.
# ============================================================================================

CAM_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


class CamOffline(Exception):
    """Room exists but is offline / private / not public right now."""


# ---------------------------------------------------------------------------------------------
# HOW TO ADD ANOTHER LIVE-CAM SITE
#   1. Write a resolver below, e.g.  def _mysite(http, url) -> list[str]
#        - ask the site's own API for the room (use http.get / http.post and _cam_get_json)
#        - raise CamOffline("reason") if the room is offline/private
#        - return a list of candidate HLS playlist addresses (.m3u8), best first
#   2. Add ONE line to CAM_SITES at the bottom of this section:  "mysite": ("mysite.com", _mysite),
#      (the first item is the text that must appear in the web address's host name)
# That's all: cam_detect(), recording and the settings pick it up automatically. A site that is not
# listed here still works if streamlink or yt-dlp already know it.
# ---------------------------------------------------------------------------------------------
def cam_detect(url: str) -> str | None:
    host = urlparse(url).netloc.lower()
    for key, (needle, _resolver) in CAM_SITES.items():
        if needle in host:
            return key
    return None


def cam_username(url: str) -> str:
    parts = [s for s in urlparse(url).path.split("/") if s]
    return parts[-1] if parts else ""


class CamHttp:
    """Small wrapper: curl_cffi (Chrome fingerprint) when available, else streamlink's session."""

    def __init__(self, sl, browser: str = "", domain: str = "", cookie_file: str = ""):
        self.sl = sl
        self.cc = None
        try:
            from curl_cffi import requests as cr
            self.cc = cr.Session(impersonate="chrome")
        except Exception:
            pass
        if cookie_file:
            try:
                import http.cookiejar as cj
                jar = cj.MozillaCookieJar(cookie_file)
                jar.load(ignore_discard=True, ignore_expires=True)
                self._add_jar(jar, domain)
            except Exception:
                pass
        if browser:
            self._load_browser_cookies(browser, domain)

    def _add_jar(self, jar, domain: str) -> None:
        for c in jar:
            if domain and domain not in (c.domain or ""):
                continue
            if self.cc is not None:
                self.cc.cookies.set(c.name, c.value, domain=c.domain)
            self.sl.http.cookies.set_cookie(c)

    def _load_browser_cookies(self, browser: str, domain: str) -> None:
        try:
            from yt_dlp.cookies import extract_cookies_from_browser
            jar = extract_cookies_from_browser(browser)
        except Exception as e:
            raise RuntimeError(
                f"Could not read cookies from {browser}: {e}. Close the browser and try again "
                f"(Firefox works most reliably; recent Chrome/Edge on Windows may block it).")
        self._add_jar(jar, domain)

    def request(self, method: str, url: str, **kw):
        headers = dict(kw.pop("headers", {}) or {})
        if self.cc is not None:
            headers.pop("User-Agent", None)  # let the impersonated browser UA stand
            return getattr(self.cc, method)(url, headers=headers, timeout=25, **kw)
        headers.setdefault("User-Agent", CAM_UA)
        return getattr(self.sl.http, method)(url, headers=headers, raise_for_status=False, **kw)

    def get(self, url, **kw):
        return self.request("get", url, **kw)

    def post(self, url, **kw):
        return self.request("post", url, **kw)


def _cam_get_json(resp, what: str) -> dict:
    if resp.status_code != 200:
        hint = " (blocked by the site / Cloudflare: try Settings > 'Use cookies from browser')" if resp.status_code in (403, 429) else ""
        raise RuntimeError(f"{what}: HTTP {resp.status_code}{hint}")
    try:
        return resp.json()
    except Exception:
        raise RuntimeError(f"{what}: response was not JSON (probably a challenge page): {resp.text[:160]!r}")


def _chaturbate(http, url: str) -> list[str]:
    user = cam_username(url)
    tok = uuid.uuid4().hex.upper()[:32]
    r = http.post(
        "https://chaturbate.com/get_edge_hls_url_ajax/",
        data={"room_slug": user, "bandwidth": "high"},
        headers={"X-CSRFToken": tok, "X-Requested-With": "XMLHttpRequest",
                 "Referer": f"https://chaturbate.com/{user}/"},
        cookies={"csrftoken": tok},
    )
    d = _cam_get_json(r, "Live-room API")
    status = str(d.get("room_status", "unknown"))
    if d.get("success") and status == "public" and d.get("url"):
        return [d["url"]]
    if not d.get("url"):
        raise CamOffline(status)
    raise RuntimeError(f"Live room: unexpected API response {str(d)[:300]}")


def _stripchat(http, url: str) -> list[str]:
    user = cam_username(url)
    host = urlparse(url).netloc or "stripchat.com"  # XHamsterLive runs on the same platform
    r = http.get(
        f"https://{host}/api/front/v2/models/username/{user}/cam",
        headers={"X-Requested-With": "XMLHttpRequest", "Referer": url},
    )
    d = _cam_get_json(r, "Live-room API")
    try:
        cam = d["cam"]
        u = d["user"]["user"]
    except Exception:
        raise CamOffline(str(d.get("message") or d.get("error") or "no cam data")[:80])
    name = cam.get("streamName")
    if not (u.get("isLive") and u.get("status") == "public" and name):
        raise CamOffline(str(u.get("status", "offline")))
    srv = (cam.get("viewServers") or {}).get("flashphoner-hls", "")
    cands = [
        f"https://edge-hls.doppiocdn.com/hls/{name}/master/{name}_auto.m3u8",
        f"https://edge-hls.doppiocdn.org/hls/{name}/master/{name}_auto.m3u8",
    ]
    if srv:
        cands += [
            f"https://b-{srv}.doppiocdn.com/hls/{name}/master/{name}_auto.m3u8",
            f"https://b-{srv}.strpst.com/hls/{name}/master_{name}.m3u8",
            f"https://b-{srv}.strpst.com/hls/{name}/{name}.m3u8",
        ]
    return cands


CAM_SITES = {
    "chaturbate": ("chaturbate.", _chaturbate),
    "stripchat": ("stripchat.", _stripchat),
    "xhamsterlive": ("xhamsterlive.", _stripchat),
}


def _cam_pick(streams: dict, quality: str):
    import re
    if quality in streams:
        return streams[quality]

    def res(n):
        m = re.match(r"(\d+)p(\d+)?", n)
        return (int(m.group(1)), int(m.group(2) or 0)) if m else (0, 0)

    ordered = sorted(streams, key=res)
    return streams[ordered[0] if quality == "worst" else ordered[-1]]


def cam_open_stream(sl, url: str, quality: str = "best", browser: str = "", cookie_file: str = ""):
    """Return a streamlink Stream for a public room, or raise CamOffline / RuntimeError."""
    from streamlink.stream.hls import HLSStream

    site = cam_detect(url)
    http = CamHttp(sl, browser, site or "", cookie_file)
    cands = CAM_SITES[site][1](http, url)
    headers = {"Referer": url, "User-Agent": CAM_UA}
    sl.set_option("http-headers", headers)
    last = None
    for c in cands:
        try:
            streams = HLSStream.parse_variant_playlist(sl, c, headers=headers)
            if streams:
                return _cam_pick(dict(streams), quality)
            return HLSStream(sl, c, headers=headers)
        except Exception as e:
            last = e
    raise RuntimeError(f"Could not open any playlist ({site}); last error: {last}")
