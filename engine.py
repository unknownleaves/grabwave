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
                "path", "error", "added", "alt_url", "alt_referer")
        return {k: getattr(self, k) for k in keys}

    @classmethod
    def from_json(cls, d: dict) -> "Item":
        it = cls(d["url"], d.get("kind", "download"), d.get("outdir", ""))
        for k, v in d.items():
            if hasattr(it, k):
                setattr(it, k, v)
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

def _common_ydl_opts(cfg: Config, use_cookies: bool = True) -> dict:
    o = {
        "quiet": True,
        "color": {"stdout": "never", "stderr": "never"},
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        "continuedl": True,
        "retries": 10,
        "fragment_retries": 10,
        "concurrent_fragment_downloads": 4,
        "windowsfilenames": True,
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
    q = cfg.quality[:-1] if cfg.quality.endswith("p") else ""
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


def run_download(item: Item, cfg: Config) -> None:
    """Try the page/media URL with yt-dlp; then again without the browser cookies (stale sign-ins break some
    sites, e.g. X/Twitter); then the stream address the browser pane captured (item.alt_url)."""
    def reset():
        item.error = ""
        item.done = item.total = 0
        item.speed = 0.0

    first = None
    try:
        _download_once(item, cfg, item.url, item.referer, False)
        return
    except Exception as ex:
        if item.stop.is_set():
            raise
        first = ex
    if effective_cookie_file(cfg) or cfg.cookies_browser:
        reset()
        try:
            _download_once(item, cfg, item.url, item.referer, False, use_cookies=False)
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
    opts = _common_ydl_opts(cfg, use_cookies)
    opts["outtmpl"] = str(Path(outdir) / "%(title).150B [%(id)s].%(ext)s")
    if item.filename:
        opts["outtmpl"] = str(Path(outdir) / (item.filename.replace("%", "%%") + ".%(ext)s"))
    if referer:
        hdr = {"Referer": referer}
        if is_alt:
            p = urlparse(referer)
            if p.scheme and p.netloc:
                hdr["Origin"] = f"{p.scheme}://{p.netloc}"
        opts["http_headers"] = hdr
    opts["progress_hooks"] = [_make_hook(item)]
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
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


def run_live_streamlink(item: Item, cfg: Config) -> None:
    from streamlink import Streamlink
    from streamlink.exceptions import NoPluginError, PluginError

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

    stream = None
    while stream is None:
        if item.stop.is_set():
            item.status = "Stopped"
            raise StopRequested()
        why = "waiting for stream"
        import camsites
        if camsites.detect(item.url):
            try:
                stream = camsites.open_stream(sl, item.url, cfg.live_quality, cfg.cookies_browser, cookie_file)
            except camsites.CamOffline as e:
                why = f"offline/not public: {e}"
            except Exception as e:  # site blocked our direct lookup: try streamlink's own plugin
                why = f"blocked/unavailable: {str(e)[:90]}"
                try:
                    streams = sl.streams(item.url)
                    if streams:
                        stream = streams.get(cfg.live_quality) or streams.get("best")
                except Exception:
                    pass
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
        import camsites
        if item.kind == "download" and camsites.detect(item.url):
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
            if "beeg." in item.url.lower() and not item.alt_url:
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
        slots = self.cfg.max_concurrent - self.active_count()
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
