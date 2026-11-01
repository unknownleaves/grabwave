"""Grabwave engine: queue manager, downloader (yt-dlp) and live recorder (streamlink / yt-dlp).

No Qt imports here so it can be tested headless.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, asdict, field
from pathlib import Path
from urllib.parse import urlparse

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
    "Maximum": dict(slots=MAX_CONCURRENT, frags=8, tick=250, threads=0, thumbs=4, prio="high", chromium=""),
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
    if item.alt_url:
        reset()
        _download_once(item, cfg, item.alt_url, item.alt_referer or item.referer or item.url, True)
        return
    raise first


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
            except Exception as e:  # site blocked our direct lookup (e.g. Stripchat HTTP 418)
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
                                  "requests).\n\nWorkaround: open the room in the Browse tab, sign in if the site "
                                  "asks, let the video start playing, then click the download button (top right "
                                  "of the browser bar). Grabwave then records the exact stream your browser is "
                                  "playing.")
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
            low = item.url.lower()
            file_trouble = "Errno" in item.error or "No such file" in item.error
            if file_trouble:
                item.error += ("\n\nHint: Windows couldn't save the file in that folder. Try a folder on your "
                               "main drive (Settings or the Change button next to Save to). Network and cloud "
                               "drives sometimes refuse downloads.")
            if ("x.com/" in low or "twitter.com/" in low) and not item.alt_url and not file_trouble:
                item.error += ("\n\nHint: X shows many videos only to signed-in accounts (adult/sensitive posts, "
                               "age-restricted posts). In the Browse tab open x.com, sign in once, then add the "
                               "link again. Grabwave reuses that sign-in. Posts that were deleted, or are from a "
                               "private account you don't follow, can't be downloaded.")
            if "beeg." in low and not item.alt_url:
                item.error += ("\n\nHint: Beeg changed its site and yt-dlp can't read it from the link alone. "
                               "Open the video in the built-in browser, let it start loading, then click the "
                               "Download button on the page (or the address-bar download button).")
    finally:
        item.speed = 0.0
        item.running = False


# ----------------------------------------------------------------------------
# Manager
# ----------------------------------------------------------------------------

class Manager:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.items: list[Item] = []
        self._threads: dict[str, threading.Thread] = {}
        self.load()
        for old in config_dir().glob("cj_*.txt"):  # leftover private cookie copies
            try:
                if time.time() - old.stat().st_mtime > 86400:
                    old.unlink()
            except OSError:
                pass

    # --- queue ------------------------------------------------------------
    def add(self, url: str, kind: str = "download", outdir: str = "") -> Item:
        it = Item(url.strip(), kind, outdir)
        self.items.append(it)
        return it

    def active_count(self) -> int:
        return sum(1 for i in self.items if i.running)

    def tick(self) -> None:
        slots = effective_slots(self.cfg) - self.active_count()
        for it in self.items:
            if slots <= 0:
                break
            if it.status == "Queued" and not it.running:
                self._start(it)
                slots -= 1

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
    d = _cam_get_json(r, "Chaturbate API")
    status = str(d.get("room_status", "unknown"))
    if d.get("success") and status == "public" and d.get("url"):
        return [d["url"]]
    if not d.get("url"):
        raise CamOffline(status)
    raise RuntimeError(f"Chaturbate: unexpected API response {str(d)[:300]}")


def _stripchat(http, url: str) -> list[str]:
    user = cam_username(url)
    host = urlparse(url).netloc or "stripchat.com"  # XHamsterLive runs on the same platform
    r = http.get(
        f"https://{host}/api/front/v2/models/username/{user}/cam",
        headers={"X-Requested-With": "XMLHttpRequest", "Referer": url},
    )
    d = _cam_get_json(r, "Stripchat API")
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
