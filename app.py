"""Grabwave GUI.

Layout modelled on MeGet: a built-in browser (with site shortcuts) on the left and a
card-style downloads panel on the right (Downloading / Finished tabs, Format / Quality /
Subtitle selectors and a file-location row at the bottom).
"""
from __future__ import annotations

import rollback  # standard library only: repairs a broken update before anything else loads

rollback.guard()

import os
import subprocess
import warnings
import sys
import time
from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets
from PySide6.QtCore import Qt

import engine as E
import ui_extra as UI
import updater as U

VERSION = "2.10"

try:  # the built-in browser needs Qt WebEngine (shipped with the PySide6 wheels)
    from PySide6.QtWebEngineCore import QWebEngineProfile, QWebEnginePage, QWebEngineScript, QWebEngineSettings
    from PySide6.QtWebEngineWidgets import QWebEngineView
    HAVE_WEB = True
except Exception:  # pragma: no cover
    HAVE_WEB = False

# Injected into every page/frame of the built-in browser. When a <video>/<audio> element is
# present it shows a floating "Download" button (like MeGet) and reports the media URL (if the
# page exposes a plain http(s) one) together with the page address to use as Referer.
# Not injected on sites that host paywalled creator content or leaked material.
BLOCKED_HOSTS = ["onlyfans.com", "fansly.com", "leakedzone.com", "fapello.com"]

DETECTOR_JS = r"""
(function () {
  if (window.__vgDetector) return;
  window.__vgDetector = true;
  var BLOCKED = __BLOCKED__;
  var EMBED = /(player\.vimeo\.com|youtube(-nocookie)?\.com\/embed|wistia\.(com|net)|dailymotion\.com\/embed|rumble\.com\/embed|streamable\.com\/e\/|ok\.ru\/videoembed|player\.twitch\.tv|loom\.com\/embed)/i;
  var host = location.hostname;
  for (var i = 0; i < BLOCKED.length; i++) {
    if (host === BLOCKED[i] || host.endsWith("." + BLOCKED[i])) return;
  }
  if (!/(^|\.)(youtube\.com|youtube-nocookie\.com|googlevideo\.com|google\.com|x\.com|twitter\.com)$/i.test(host)) {
    try {
      var seenU = {};
      var po = new PerformanceObserver(function (list) {
        list.getEntries().forEach(function (en) {
          var p = en.name.split("#")[0].split("?")[0].toLowerCase();
          if (/\.(m3u8|mpd|mp4|webm|m4v)$/.test(p) && !seenU[en.name]) {
            seenU[en.name] = 1; console.log("GRABWAVE-SNIFF:" + en.name);
          }
        });
      });
      po.observe({ type: "resource", buffered: true });
    } catch (err) {}
  }
  var btn = null;
  function currentMedia() {
    var els = document.querySelectorAll("video, audio");
    var direct = null, embed = null, fv = null;
    for (var i = 0; i < els.length && !direct; i++) {
      var src = pickSource(els[i]);
      if (src && /^https?:/i.test(src)) direct = src;   // blob: (MSE) streams can't be saved directly
    }
    // some players (e.g. KVS) publish a plain address in a global; encoded ones are ignored
    try {
      if (window.flashvars && typeof window.flashvars.video_url === "string" &&
          /^https?:/i.test(window.flashvars.video_url)) { fv = window.flashvars.video_url; }
    } catch (err) {}
    if (!direct && fv) direct = fv;
    if (!els.length) {                                   // no <video>: look for an embedded player
      var fr = document.querySelectorAll("iframe");
      for (var j = 0; j < fr.length && !embed; j++) {
        if (fr[j].src && EMBED.test(fr[j].src)) embed = fr[j].src;
      }
    }
    if (!els.length && !embed && !fv) return null;
    return { direct: direct, embed: embed };
  }
  // prefer the best-labelled <source> ("original", or the largest size/res) over the playing one
  function pickSource(e) {
    var playing = e.currentSrc || e.src || null, cand = null, best = 0;
    var ss = e.querySelectorAll ? e.querySelectorAll("source") : [];
    for (var k = 0; k < ss.length; k++) {
      var s = ss[k];
      if (!s.src) continue;
      var lbl = (s.getAttribute("label") || "").toLowerCase();
      var sc = lbl === "original" ? 1e9 : (parseInt(s.getAttribute("size") || s.getAttribute("res") || "0", 10) || 0);
      if (sc > best) { best = sc; cand = s.src; }
    }
    if (cand) return cand;
    return playing || (ss.length ? ss[0].src : null);
  }
  function ensure(info) {
    if (!info) { if (btn) { btn.remove(); btn = null; } return; }
    if (btn) return;
    btn = document.createElement("button");
    btn.id = "grabwave-btn";
    btn.textContent = "⬇ Download";
    btn.style.cssText = "position:fixed;bottom:30px;right:30px;z-index:2147483647;padding:8px 15px;" +
      "font:600 15px sans-serif;color:#fff;background:#5b3df5;border:none;border-radius:5px;" +
      "cursor:pointer;box-shadow:0 1px 3px rgba(0,0,0,.25)";
    btn.onclick = function (ev) {
      ev.stopPropagation(); ev.preventDefault();
      var cur = currentMedia() || { direct: null, embed: null };
      // Players often live in an iframe (Dailymotion, many others). The page address people paste is the TOP page.
      var inFrame = false, topUrl = location.href, topTitle = document.title;
      try { inFrame = !!window.top && window !== window.top; } catch (err) { inFrame = true; }
      if (inFrame) {
        try { topUrl = window.top.location.href; topTitle = window.top.document.title || topTitle; }
        catch (err) { topUrl = document.referrer || location.href; }
      }
      var payload;
      if (cur.direct) {
        payload = { media: cur.direct, page: location.href, title: topTitle, referer: location.href };
      } else {
        payload = { media: null, page: inFrame ? topUrl : (cur.embed || location.href),
                    embed: inFrame ? location.href : (cur.embed || null), embedReferer: topUrl,
                    title: topTitle, referer: inFrame ? topUrl : location.href };
      }
      console.log("GRABWAVE:" + JSON.stringify(payload));
      btn.textContent = "✓ Added";
      setTimeout(function () { if (btn) btn.textContent = "⬇ Download"; }, 1500);
    };
    document.documentElement.appendChild(btn);
  }
  setInterval(function () { ensure(currentMedia()); }, 700);
})();
"""

import json as _json
DETECTOR_JS = DETECTOR_JS.replace("__BLOCKED__", _json.dumps(BLOCKED_HOSTS))


def resource_path(rel: str) -> Path:
    """Files bundled with the app (works from source and from the installed/frozen build)."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / rel


SNIFF_SKIP = ("preview", "thumb", "trailer", "sprite", "teaser", "poster", "doubleclick", "adserver", "/ads/")
NO_AUTO_CAPTURE = ("youtube.com", "youtu.be", "youtube-nocookie.com", "google.com", "x.com", "twitter.com")


PREVIEW_WORDS = ("preview", "trailer", "teaser", "thumb", "sample", "promo", "banner", "sprite", "poster",
                 "storyboard", "/ads/", "_ad_", "intro", "short_", "_short", "clip_", "gif")


def is_preview(u: str) -> bool:
    """Short preview clips, trailers and ads that many pages load next to the real video."""
    low = u.lower().split("?")[0]
    return any(w in low for w in PREVIEW_WORDS)


def direct_size(url: str, referer: str) -> int:
    """File size of a direct video address (0 if unknown). Asks for one byte, so it is quick."""
    import ssl
    import urllib.request
    try:
        try:
            import certifi
            ctx = ssl.create_default_context(cafile=certifi.where())
        except Exception:
            ctx = ssl.create_default_context()
        req = urllib.request.Request(url, headers={
            "Range": "bytes=0-0", "Referer": referer, "User-Agent": E.browser_ua() or "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=8, context=ctx) as r:
            cr = r.headers.get("Content-Range", "")
            if "/" in cr and cr.rsplit("/", 1)[1].isdigit():
                return int(cr.rsplit("/", 1)[1])
            return int(r.headers.get("Content-Length") or 0)
    except Exception:
        return 0


def pick_best_media(urls: list, referer: str = "", probe: bool = False) -> str:
    """The stream address most likely to be the full video: a master playlist, else the first playlist,
    else the biggest direct file (previews and trailers are ignored when anything else was found)."""
    def ext(u): return u.lower().split("?")[0]
    real = [u for u in urls if not is_preview(u)] or list(urls)
    pls = [u for u in real if ext(u).endswith(".m3u8")]
    if pls:
        masters = [u for u in pls if "master" in u.lower()]
        return (masters or pls)[0]
    mpd = [u for u in real if ext(u).endswith(".mpd")]
    if mpd:
        return mpd[0]
    files = real[-6:]
    if probe and len(files) > 1:  # several direct files: the largest one is the full video in the best quality
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=len(files)) as ex:
            sizes = list(ex.map(lambda u: direct_size(u, referer), files))
        if max(sizes) > 0:
            return files[sizes.index(max(sizes))]
    return real[-1] if real else ""


def is_blocked_host(url: str) -> bool:
    from urllib.parse import urlparse
    host = (urlparse(url).hostname or "").lower()
    return any(host == b or host.endswith("." + b) for b in BLOCKED_HOSTS)


if HAVE_WEB:
    class MediaView(QWebEngineView):
        """Web view with extra right-click entries: add a link / media item to the download list."""
        mediaRequested = QtCore.Signal(dict)
        linkRequested = QtCore.Signal(str)

        def contextMenuEvent(self, event):
            req = self.lastContextMenuRequest()
            try:
                menu = self.createStandardContextMenu()
            except Exception:
                menu = QtWidgets.QMenu(self)
            page_url = self.url().toString()
            if req is not None and not is_blocked_host(page_url):
                link = req.linkUrl().toString()
                media = req.mediaUrl().toString()
                if media.lower().startswith(("http://", "https://")):
                    menu.addSeparator()
                    menu.addAction("Add this media to Grabwave downloads", lambda m=media: self.mediaRequested.emit(
                        {"media": m, "page": page_url, "title": self.title(), "referer": page_url}))
                if link.lower().startswith(("http://", "https://")):
                    menu.addSeparator()
                    menu.addAction("Add this link to Grabwave downloads", lambda u=link: self.linkRequested.emit(u))
            menu.popup(event.globalPos())

    class MediaPage(QWebEnginePage):
        """Web page that listens for the detector script's console messages."""
        mediaFound = QtCore.Signal(dict)
        sniffed = QtCore.Signal(str)

        _popups: list = []

        def createWindow(self, wtype):
            """Sign-in buttons ("Continue with Google/Apple") open a small pop-up window. Give it a real
            window that shares this page's sign-ins, and close it when the site asks."""
            dlg = QtWidgets.QDialog(self.view().window() if self.view() else None)
            dlg.setWindowTitle("Sign in")
            dlg.resize(520, 700)
            lay = QtWidgets.QVBoxLayout(dlg)
            lay.setContentsMargins(0, 0, 0, 0)
            view = QWebEngineView(dlg)
            page = QWebEnginePage(self.profile(), view)
            view.setPage(page)
            lay.addWidget(view)
            page.windowCloseRequested.connect(dlg.close)
            MediaPage._popups.append(dlg)
            dlg.finished.connect(lambda _=0, d=dlg: MediaPage._popups.remove(d) if d in MediaPage._popups else None)
            dlg.show()
            return page

        def javaScriptConsoleMessage(self, level, message, line, source):
            if message.startswith("GRABWAVE-SNIFF:"):
                self.sniffed.emit(message[len("GRABWAVE-SNIFF:"):])
                return
            if message.startswith("GRABWAVE:"):
                import json
                try:
                    self.mediaFound.emit(json.loads(message[len("GRABWAVE:"):]))
                except ValueError:
                    pass
                return
            super().javaScriptConsoleMessage(level, message, line, source)

    class PageSniffer(QtCore.QObject):
        """Opens a page invisibly (with the browser pane's sign-ins), presses play, and reports the stream
        address the page loads. Used automatically when a link can't be downloaded the normal way."""
        finished = QtCore.Signal(str)

        def __init__(self, profile, url: str, parent=None):
            super().__init__(parent)
            self.found: list = []
            self.n = 0
            self.page_url = url
            self.view = QWebEngineView()
            page = MediaPage(profile, self.view)
            page.sniffed.connect(self._seen)
            script = QWebEngineScript()
            script.setName("grabwave-detector")
            script.setSourceCode(DETECTOR_JS)
            script.setInjectionPoint(QWebEngineScript.DocumentReady)
            script.setWorldId(QWebEngineScript.MainWorld)
            script.setRunsOnSubFrames(True)
            page.scripts().insert(script)
            try:
                page.settings().setAttribute(QWebEngineSettings.PlaybackRequiresUserGesture, False)
            except Exception:
                pass
            self.view.setPage(page)
            self.view.resize(1280, 720)
            self.view.setAttribute(Qt.WA_DontShowOnScreen, True)
            self.view.show()
            self.view.load(QtCore.QUrl(url))
            self.timer = QtCore.QTimer(self)
            self.timer.timeout.connect(self._step)
            self.timer.start(1000)

        def _seen(self, u: str):
            if any(w in u.lower() for w in SNIFF_SKIP):
                return
            if u not in self.found:
                self.found.append(u)

        def _step(self):
            self.n += 1
            try:
                self.view.page().runJavaScript(
                    "document.querySelectorAll('video').forEach(function(v){v.muted=true;"
                    "if(v.play){var p=v.play();if(p&&p.catch){p.catch(function(){});}}});"
                    "var b=document.querySelector('.vjs-big-play-button,.play-button,.big-play-button,"
                    "button[aria-label*=\"lay\"]');if(b){b.click();}"
                    # also read plain video addresses straight from the page (file-hosting sites)
                    "document.querySelectorAll('video,source,meta[property=\"og:video\"],"
                    "meta[property=\"og:video:url\"],a[href*=\".mp4\"],a[href*=\".m3u8\"],"
                    "a[href*=\".webm\"]').forEach(function(e){var u=e.currentSrc||e.src||e.content||e.href;"
                    "if(u&&/^https?:/.test(u)&&/\\.(mp4|m3u8|webm|m4v|mpd)([?#]|$)/i.test(u)){"
                    "console.log('GRABWAVE-SNIFF:'+u);}});")
            except Exception:
                pass
            real = [u for u in self.found if not is_preview(u)]
            if (real and self.n >= 6) or self.n >= 25:
                self.stop(True)

        def stop(self, emit: bool = True):
            self.timer.stop()
            try:
                self.view.setPage(None)
            except Exception:
                pass
            self.view.deleteLater()
            if not emit:
                return
            import threading
            result: list = []
            found, ref = list(self.found), self.page_url

            def work():
                try:
                    result.append(pick_best_media(found, ref, True))
                except Exception:  # noqa: BLE001
                    result.append(found[-1] if found else "")

            threading.Thread(target=work, daemon=True).start()
            poll = QtCore.QTimer(self)

            def check():
                if result:
                    poll.stop()
                    self.finished.emit(result[0])

            poll.timeout.connect(check)
            poll.start(150)

ACCENT, RED, GRAY = UI.ACCENT, UI.RED, UI.GRAY


def open_path(p: str) -> None:
    if sys.platform.startswith("win"):
        os.startfile(p)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", p])
    else:
        subprocess.Popen(["xdg-open", p])


# ============================================================================
# Left side: built-in browser
# ============================================================================

class BrowserPane(QtWidgets.QWidget):
    addRequested = QtCore.Signal(str)
    mediaFound = QtCore.Signal(dict)

    def __init__(self, cfg: E.Config):
        super().__init__()
        self.setObjectName("left")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.cfg = cfg
        self.web = None
        self._cookies: dict = {}

        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        bar = QtWidgets.QHBoxLayout()
        bar.setContentsMargins(18, 8, 18, 8)
        bar.setSpacing(4)

        def btn(glyph, tip, slot, kind="nav"):
            b = QtWidgets.QToolButton()
            b.setObjectName(kind)
            b.setIcon(UI.icon(glyph, "#ffffff" if kind == "grab" else UI.MUTED, 20))
            b.setIconSize(QtCore.QSize(20, 20))
            b.setFixedSize(36, 36)
            b.setToolTip(tip)
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(slot)
            bar.addWidget(b)
            return b

        self.b_home = btn("home", "Home", self.go_home)
        self.b_back = btn("back", "Back", lambda: self.web and self.web.back())
        self.b_fwd = btn("forward", "Forward", lambda: self.web and self.web.forward())
        self.b_reload = btn("reload", "Reload", lambda: self.web and self.web.reload())
        self.url = QtWidgets.QLineEdit()
        self.url.setObjectName("urlbar")
        self.url.setPlaceholderText("Search or enter a web address")
        self.url.returnPressed.connect(lambda: self.navigate(self.url.text()))
        bar.addWidget(self.url, 1)
        self.b_add = btn("download", "Grab the video on this page", self._emit_add, "grab")
        self.b_ext = btn("external", "Open in your default browser", self._open_external)
        lay.addLayout(bar)

        self.stack = QtWidgets.QStackedWidget()
        self.home = UI.HomePage(cfg)
        self.home.siteClicked.connect(self.navigate)
        self.stack.addWidget(self.home)
        lay.addWidget(self.stack, 1)

        if HAVE_WEB:
            self.profile = QWebEngineProfile("grabwave", self)
            base = E.config_dir() / "web"
            base.mkdir(parents=True, exist_ok=True)
            self.profile.setPersistentStoragePath(str(base))
            self.profile.setCachePath(str(base / "cache"))
            self.profile.setPersistentCookiesPolicy(QWebEngineProfile.ForcePersistentCookies)
            import re
            self.profile.setHttpUserAgent(re.sub(r"\s*QtWebEngine/[\d.]+", "", self.profile.httpUserAgent()))
            try:  # let the recorder use the same User-Agent as this browser
                (E.config_dir() / "browser_ua.txt").write_text(self.profile.httpUserAgent(), "utf-8")
            except OSError:
                pass
            self.web = MediaView()
            self.web.mediaRequested.connect(self.mediaFound.emit)
            self.web.linkRequested.connect(self.addRequested.emit)
            page = MediaPage(self.profile, self.web)
            page.mediaFound.connect(self._on_media)
            page.sniffed.connect(lambda u: self._on_sniff(u, self.current_url()))
            self.sniffed: list[str] = []
            self.web.loadStarted.connect(self.sniffed.clear)
            script = QWebEngineScript()
            script.setName("grabwave-detector")
            script.setSourceCode(DETECTOR_JS)
            script.setInjectionPoint(QWebEngineScript.DocumentReady)
            script.setWorldId(QWebEngineScript.MainWorld)
            script.setRunsOnSubFrames(True)
            page.scripts().insert(script)
            self.web.setPage(page)
            self.web.urlChanged.connect(lambda u: self.url.setText(u.toString()))
            self.stack.addWidget(self.web)
            # keep a Netscape-format copy of the pane's cookies so downloads can reuse the same sign-ins
            store = self.profile.cookieStore()
            store.cookieAdded.connect(self._cookie_added)
            store.cookieRemoved.connect(self._cookie_removed)
            store.loadAllCookies()
            self._cookie_timer = QtCore.QTimer(self)
            self._cookie_timer.setSingleShot(True)
            self._cookie_timer.timeout.connect(self._write_cookies)
        self._sync_buttons()

    # --- navigation -------------------------------------------------------
    def go_home(self):
        self.stack.setCurrentIndex(0)
        self.url.clear()
        self._sync_buttons()

    def navigate(self, text: str):
        text = (text or "").strip()
        if not text:
            return
        if " " in text or ("." not in text and "://" not in text and "localhost" not in text):
            q = QtCore.QUrl.toPercentEncoding(text).data().decode()
            text = f"https://duckduckgo.com/?q={q}"
        elif "://" not in text:
            text = "https://" + text
        if self.web is not None:
            self.web.setUrl(QtCore.QUrl(text))
            self.stack.setCurrentWidget(self.web)
            self.url.setText(text)
        else:
            QtGui.QDesktopServices.openUrl(QtCore.QUrl(text))
        self._sync_buttons()

    def current_url(self) -> str:
        if self.web is not None and self.stack.currentWidget() is self.web:
            return self.web.url().toString()
        return self.url.text().strip()

    def _emit_add(self):
        u = self.current_url()
        if not u.lower().startswith(("http://", "https://")):
            return
        alt = self.best_media()
        if alt and self.web is not None:
            self.mediaFound.emit({"media": "", "page": u, "title": self.web.title(), "referer": "",
                                  "alt": alt, "alt_referer": u})
        else:
            self.addRequested.emit(u)

    # --- stream sniffing ------------------------------------------------------
    def _on_sniff(self, url: str, first_party: str):
        low = url.lower()
        if is_blocked_host(first_party) or any(w in low for w in SNIFF_SKIP):
            return
        if url not in self.sniffed:
            self.sniffed.append(url)
            del self.sniffed[:-40]

    def best_media(self) -> str:
        if self.web is None:
            return ""
        return pick_best_media(self.sniffed)

    def _on_media(self, info: dict):
        info = dict(info)
        alt = self.best_media()
        if alt and alt != (info.get("media") or ""):
            info["alt"] = alt
            info["alt_referer"] = info.get("page") or self.current_url()
        self.mediaFound.emit(info)

    def _open_external(self):
        u = self.current_url()
        if u:
            QtGui.QDesktopServices.openUrl(QtCore.QUrl(u))

    def _sync_buttons(self):
        on_web = self.web is not None
        for b in (self.b_back, self.b_fwd, self.b_reload):
            b.setEnabled(on_web)

    # --- cookie export ------------------------------------------------------
    @staticmethod
    def _key(c):
        return (c.domain(), c.path(), bytes(c.name()).decode("utf-8", "replace"))

    def _cookie_added(self, c):
        self._cookies[self._key(c)] = c
        self._cookie_timer.start(1500)

    def _cookie_removed(self, c):
        self._cookies.pop(self._key(c), None)
        self._cookie_timer.start(1500)

    def _write_cookies(self):
        lines = ["# Netscape HTTP Cookie File"]
        now = int(time.time())
        for c in list(self._cookies.values()):
            domain = c.domain()
            exp = now + 86400 if c.isSessionCookie() else int(c.expirationDate().toSecsSinceEpoch())
            name = bytes(c.name()).decode("utf-8", "replace")
            value = bytes(c.value()).decode("utf-8", "replace")
            prefix = "#HttpOnly_" if c.isHttpOnly() else ""
            lines.append("\t".join([
                prefix + domain, "TRUE" if domain.startswith(".") else "FALSE", c.path() or "/",
                "TRUE" if c.isSecure() else "FALSE", str(exp), name, value]))
        try:
            path = E.builtin_cookie_path()
            tmp = path.with_suffix(".tmp")
            tmp.write_text("\n".join(lines) + "\n", "utf-8")
            os.replace(tmp, path)
        except OSError:
            pass


# ============================================================================
# Right side: downloads list (card delegate)
# ============================================================================

class ListModel(QtCore.QAbstractListModel):
    def __init__(self, mgr: E.Manager):
        super().__init__()
        self.mgr = mgr

    def rowCount(self, parent=QtCore.QModelIndex()):
        return 0 if parent.isValid() else len(self.mgr.items)

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid() or index.row() >= len(self.mgr.items):
            return None
        it = self.mgr.items[index.row()]
        if role == Qt.UserRole:
            return it
        if role == Qt.ToolTipRole:
            return it.url + (f"\n\n{it.error[:600]}" if it.error else "")
        if role == Qt.DisplayRole:
            return it.title
        return None

    def reset(self):
        self.beginResetModel()
        self.endResetModel()

    def refresh(self):
        n = len(self.mgr.items)
        if n:
            self.dataChanged.emit(self.index(0, 0), self.index(n - 1, 0))


class FilterProxy(QtCore.QSortFilterProxyModel):
    def __init__(self):
        super().__init__()
        self.pred = lambda i: True

    def filterAcceptsRow(self, row, parent):
        items = self.sourceModel().mgr.items
        return row < len(items) and self.pred(items[row])


# ============================================================================
# Dialogs
# ============================================================================

class AddDialog(QtWidgets.QDialog):
    def __init__(self, parent, cfg: E.Config, live: bool):
        super().__init__(parent)
        self.cfg = cfg
        self.setWindowTitle("Add URLs")
        self.resize(560, 340)
        lay = QtWidgets.QVBoxLayout(self)
        lay.addWidget(QtWidgets.QLabel("Paste one URL per line (up to 20 run at the same time):"))
        self.text = QtWidgets.QPlainTextEdit()
        lay.addWidget(self.text, 1)
        clip = QtWidgets.QApplication.clipboard().text()
        if "http" in clip:
            self.text.setPlainText("\n".join(l for l in clip.splitlines() if l.strip().startswith("http")))
        form = QtWidgets.QFormLayout()
        self.mode = QtWidgets.QComboBox()
        self.mode.addItems(["Download video", "Record live stream"])
        self.mode.setCurrentIndex(1 if live else 0)
        form.addRow("Mode:", self.mode)
        lay.addLayout(form)
        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def urls(self) -> list[str]:
        return [l.strip() for l in self.text.toPlainText().splitlines()
                if l.strip().lower().startswith(("http://", "https://"))]

    def kind(self) -> str:
        return "live" if self.mode.currentIndex() == 1 else "download"


class SettingsDialog(QtWidgets.QDialog):
    def __init__(self, parent, cfg: E.Config):
        super().__init__(parent)
        self.cfg = cfg
        self.setWindowTitle("Settings")
        self.resize(620, 0)
        form = QtWidgets.QFormLayout(self)

        perf_box = QtWidgets.QGroupBox("Performance")
        pl = QtWidgets.QVBoxLayout(perf_box)
        pl.setSpacing(4)
        self.perf_radios = {}
        labels = {"Maximum": "Maximum (use all the resources it needs)", "Balanced": "Balanced (recommended)",
                  "Light": "Light (use fewer resources)"}
        grp = QtWidgets.QButtonGroup(self)
        current = cfg.performance if cfg.performance in E.PERFORMANCE else "Balanced"
        for name in ("Maximum", "Balanced", "Light"):
            rb = QtWidgets.QRadioButton(labels[name])
            rb.setChecked(name == current)
            grp.addButton(rb)
            self.perf_radios[name] = rb
            note = QtWidgets.QLabel(E.PERFORMANCE_NOTES[name])
            note.setWordWrap(True)
            note.setStyleSheet(f"color: {UI.MUTED}; margin-left: 24px; margin-bottom: 6px; font-weight: 400;")
            pl.addWidget(rb)
            pl.addWidget(note)
        hint = QtWidgets.QLabel("Speed and priority change right away. The browser part changes after Grabwave restarts.")
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color: {UI.FAINT}; font-weight: 400;")
        pl.addWidget(hint)
        form.addRow(perf_box)

        w = QtWidgets.QWidget()
        h = QtWidgets.QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        self.live = QtWidgets.QLineEdit(cfg.live_dir)
        b = QtWidgets.QPushButton("Browse…")
        b.clicked.connect(lambda: self.live.setText(
            QtWidgets.QFileDialog.getExistingDirectory(self, "Folder", self.live.text()) or self.live.text()))
        h.addWidget(self.live, 1)
        h.addWidget(b)
        form.addRow("Recordings folder:", w)

        self.maxc = QtWidgets.QSpinBox()
        self.maxc.setRange(1, E.MAX_CONCURRENT)
        self.maxc.setValue(cfg.max_concurrent)
        form.addRow(f"Most at once (max {E.MAX_CONCURRENT}; the performance mode may lower it):", self.maxc)

        self.quality = QtWidgets.QLineEdit(cfg.live_quality)
        self.quality.setPlaceholderText("best, worst, 720p, 1080p60 …")
        form.addRow("Live quality:", self.quality)

        self.poll = QtWidgets.QSpinBox()
        self.poll.setRange(5, 600)
        self.poll.setSuffix(" s")
        self.poll.setValue(cfg.live_poll_seconds)
        form.addRow("Check offline channels every:", self.poll)

        self.smart = QtWidgets.QCheckBox("Offer to grab links I copy (a small card appears)")
        self.smart.setChecked(cfg.smart_paste)
        form.addRow("", self.smart)
        self.tray_cb = QtWidgets.QCheckBox("Keep running in the system tray when I close the window (needed for the watchlist)")
        self.tray_cb.setChecked(cfg.close_to_tray)
        form.addRow("", self.tray_cb)
        self.auto_eng = QtWidgets.QCheckBox("Keep the video engine up to date automatically")
        self.auto_eng.setChecked(cfg.auto_engine)
        form.addRow("", self.auto_eng)
        self.upd = QtWidgets.QCheckBox("Check for updates when Grabwave opens")
        self.upd.setChecked(cfg.check_updates)
        form.addRow("", self.upd)
        self.sounds = QtWidgets.QCheckBox("Play a sound when something finishes")
        self.sounds.setChecked(cfg.sounds)
        form.addRow("", self.sounds)

        self.remux = QtWidgets.QCheckBox("Convert finished recordings (.ts) to .mp4 (needs ffmpeg)")
        self.remux.setChecked(cfg.remux_mp4)
        form.addRow("", self.remux)

        self.builtin = QtWidgets.QCheckBox("Use the sign-ins from the built-in browser for downloads")
        self.builtin.setChecked(cfg.use_builtin_cookies)
        form.addRow("", self.builtin)

        self.cookies = QtWidgets.QLineEdit(cfg.cookies_file)
        self.cookies.setPlaceholderText("Optional: cookies.txt (Netscape format)")
        form.addRow("Cookies file:", self.cookies)

        self.browser = QtWidgets.QComboBox()
        self.browser.addItems(["(off)", "firefox", "chrome", "edge", "brave", "chromium", "opera", "vivaldi"])
        self.browser.setCurrentText(cfg.cookies_browser or "(off)")
        form.addRow("Use cookies from browser:", self.browser)

        self.proxy = QtWidgets.QLineEdit(cfg.proxy)
        self.proxy.setPlaceholderText("Optional: http://host:port or socks5://host:port")
        form.addRow("Proxy:", self.proxy)

        ew = QtWidgets.QWidget()
        eh = QtWidgets.QHBoxLayout(ew)
        eh.setContentsMargins(0, 0, 0, 0)
        self.eng_label = QtWidgets.QLabel(f"yt-dlp {E.ytdlp_version() or 'unknown'}")
        self.eng_chan = QtWidgets.QComboBox()
        self.eng_chan.addItems(["Nightly (fixes sites fastest)", "Stable"])
        self.eng_btn = QtWidgets.QPushButton("Update now")
        self.eng_btn.clicked.connect(self._update_engine)
        eh.addWidget(self.eng_label, 1)
        eh.addWidget(self.eng_chan)
        eh.addWidget(self.eng_btn)
        form.addRow("Video engine:", ew)

        ff = E.find_ffmpeg()
        form.addRow("ffmpeg:", QtWidgets.QLabel(ff or "not found (reinstall Grabwave, or put ffmpeg next to the app)"))

        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        bb.accepted.connect(self._ok)
        bb.rejected.connect(self.reject)
        form.addRow(bb)

    def _update_engine(self):
        """Download the newest yt-dlp in the background (it is used after Grabwave restarts)."""
        import threading
        self.eng_btn.setEnabled(False)
        self.eng_label.setText("Downloading the latest yt-dlp…")
        result: list = []
        chan = "nightly" if self.eng_chan.currentIndex() == 0 else "stable"
        proxy = self.proxy.text().strip()

        def work():
            try:
                result.append(("ok", E.update_ytdlp(chan, proxy)))
            except Exception as e:  # noqa: BLE001
                result.append(("err", str(e)))

        threading.Thread(target=work, daemon=True).start()
        timer = QtCore.QTimer(self)

        def poll():
            if not result:
                return
            timer.stop()
            self.eng_btn.setEnabled(True)
            kind, val = result[0]
            if kind == "ok":
                self.eng_label.setText(f"Downloaded yt-dlp {val}. Restart Grabwave to use it.")
            else:
                self.eng_label.setText("Update failed")
                QtWidgets.QMessageBox.warning(self, "Update failed", val)

        timer.timeout.connect(poll)
        timer.start(300)

    def _ok(self):
        c = self.cfg
        c.live_dir = self.live.text().strip()
        c.max_concurrent = self.maxc.value()
        c.performance = next((n for n, rb in self.perf_radios.items() if rb.isChecked()), "Balanced")
        c.smart_paste, c.close_to_tray, c.sounds = self.smart.isChecked(), self.tray_cb.isChecked(), self.sounds.isChecked()
        c.check_updates = self.upd.isChecked()
        c.auto_engine = self.auto_eng.isChecked()
        c.live_quality = self.quality.text().strip() or "best"
        c.live_poll_seconds = self.poll.value()
        c.remux_mp4 = self.remux.isChecked()
        c.use_builtin_cookies = self.builtin.isChecked()
        c.cookies_file, c.proxy = self.cookies.text().strip(), self.proxy.text().strip()
        b = self.browser.currentText()
        c.cookies_browser = "" if b == "(off)" else b
        c.save()
        self.accept()


# ============================================================================
# Main window
# ============================================================================

class TrayView(QtWidgets.QListView):
    """The download cards; tells the window when its width changes so the columns can be recomputed."""
    resized = QtCore.Signal()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self.resized.emit()


class MainWindow(QtWidgets.QMainWindow):
    toolFinished = QtCore.Signal(object, str, str)  # source item, new file, error text
    updateChecked = QtCore.Signal(object, str, bool)  # update entry or None, error text, asked by the user

    def __init__(self, mgr: E.Manager):
        super().__init__()
        self.mgr = mgr
        self.cfg = mgr.cfg
        self.setWindowTitle(f"Grabwave {VERSION}")
        self.resize(1200, 800)
        self.setMinimumSize(900, 620)
        self.setAcceptDrops(True)
        self._quitting = False
        self._tray_hinted = False
        self._last_offer = ""
        self._seen_running: set = set()
        self._announced: set = {i.id for i in mgr.items if i.status in ("Completed", "Failed")}
        self._tick_n = 0
        self._auto_tried: set = set()
        self._sniffers: list = []

        self.thumbs = UI.ThumbCache(self.cfg)
        self.thumbs.ready.connect(lambda _id: self.view.viewport().update())
        self.watcher = UI.Watcher(mgr, self.cfg)
        self.watcher.wentLive.connect(self._went_live)
        self.toolFinished.connect(self._on_tool)
        self.updateChecked.connect(self._on_update_checked)
        self._update_open = False

        central = QtWidgets.QWidget()
        central.setObjectName("page")
        central.setAttribute(Qt.WA_StyledBackground, True)
        v = QtWidgets.QVBoxLayout(central)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        v.addWidget(self._build_header())
        v.addWidget(UI.WaveLine())

        self.pages = QtWidgets.QStackedWidget()
        self.browser = BrowserPane(self.cfg)
        self.browser.addRequested.connect(self._add_from_browser)
        self.browser.mediaFound.connect(self._add_media)
        self.browser.home.grabRequested.connect(lambda u: self._add_url(u))
        self.shelf = UI.ShelfPage(mgr, self.thumbs)
        self.shelf.openRequested.connect(self._shelf_open)
        self.shelf.folderRequested.connect(self._shelf_folder)
        self.shelf.convertRequested.connect(self._shelf_convert)
        self.shelf.trimRequested.connect(self._shelf_trim)
        self.shelf.removeRequested.connect(lambda it, d: self._remove([it], d))
        self.watch = UI.WatchPage(self.cfg, self.watcher)
        self.pages.addWidget(self.browser)
        self.pages.addWidget(self.shelf)
        self.pages.addWidget(self.watch)
        v.addWidget(self.pages, 1)
        v.addWidget(self._build_tray())
        self.setCentralWidget(central)

        self.toast = UI.SmartToast(central, self.cfg)
        self.toast.grab.connect(lambda u, q: self._add_url(u, q))
        self.splash = UI.Splash(central)

        self.tray_icon = None
        if QtWidgets.QSystemTrayIcon.isSystemTrayAvailable():
            self.tray_icon = QtWidgets.QSystemTrayIcon(QtWidgets.QApplication.windowIcon(), self)
            self._tray_menu = QtWidgets.QMenu()  # kept on self so it isn't garbage-collected
            self._tray_menu.addAction("Open Grabwave", self._show_from_tray)
            self._tray_menu.addSeparator()
            self._tray_menu.addAction("Quit", self._quit_app)
            self.tray_icon.setContextMenu(self._tray_menu)
            self.tray_icon.setToolTip("Grabwave")
            self.tray_icon.activated.connect(
                lambda r: self._show_from_tray() if r == QtWidgets.QSystemTrayIcon.Trigger else None)
            self.tray_icon.show()

        QtWidgets.QApplication.clipboard().dataChanged.connect(self._clip_changed)
        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.saver = QtCore.QTimer(self)
        self.saver.timeout.connect(mgr.save)
        self.saver.start(10000)
        self._apply_perf()
        self._tab_changed()
        self._apply_tray_state()
        self._layout_cards()
        self._tick()
        urls = self._clipboard_urls()  # a link already on the clipboard: show it in the paste bar
        self.browser.home.set_clipboard_url(urls[0] if urls else "")
        QtCore.QTimer.singleShot(15000, rollback.mark_healthy)  # the new version started fine
        QtWidgets.QApplication.instance().aboutToQuit.connect(rollback.mark_healthy)
        if self.cfg.auto_engine:
            QtCore.QTimer.singleShot(9000, self._auto_engine)
        if self.cfg.check_updates:  # a few seconds after opening, quietly look for a newer version
            QtCore.QTimer.singleShot(4000, lambda: self._check_updates(False))

    # --- header and tray ---------------------------------------------------
    def _build_header(self) -> QtWidgets.QWidget:
        w = QtWidgets.QWidget()
        h = QtWidgets.QHBoxLayout(w)
        h.setContentsMargins(22, 12, 18, 8)
        h.setSpacing(6)
        logo = QtWidgets.QLabel("grabwave")
        logo.setFont(UI.serif(17))
        logo.setStyleSheet(f"color: {UI.ACCENT};")
        h.addWidget(logo)
        h.addSpacing(14)
        grp = QtWidgets.QButtonGroup(self)
        grp.setExclusive(True)
        self.tabs = []
        for i, name in enumerate(("Browse", "Shelf", "Live")):
            b = QtWidgets.QPushButton(name)
            b.setObjectName("tab")
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            grp.addButton(b, i)
            h.addWidget(b)
            self.tabs.append(b)
        self.tabs[0].setChecked(True)
        grp.idClicked.connect(self._page_changed)
        h.addStretch(1)
        self.mascot = UI.Wavy("sleepy", 34)
        h.addWidget(self.mascot)
        helpb = QtWidgets.QToolButton()
        helpb.setText("Help")
        helpb.setIcon(UI.icon("help", UI.MUTED, 20))
        helpb.setIconSize(QtCore.QSize(20, 20))
        helpb.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        helpb.setStyleSheet("QToolButton { font-size: 13px; font-weight: 600; padding: 5px 10px; }")
        helpb.setCursor(Qt.PointingHandCursor)
        helpb.setToolTip("Help and updates")
        helpb.setPopupMode(QtWidgets.QToolButton.InstantPopup)
        hm = QtWidgets.QMenu(helpb)
        hm.addAction("Check for updates…", lambda: self._check_updates(True))
        hm.addSeparator()
        hm.addAction("Quick guide", self._guide)
        hm.addAction("Open log / settings folder", lambda: open_path(str(E.config_dir())))
        hm.addSeparator()
        hm.addAction("About Grabwave", self._about)
        helpb.setMenu(hm)
        h.addWidget(helpb)
        gear = QtWidgets.QToolButton()
        gear.setIcon(UI.icon("sliders", UI.MUTED, 20))
        gear.setIconSize(QtCore.QSize(20, 20))
        gear.setToolTip("Settings")
        gear.clicked.connect(self._settings)
        h.addWidget(gear)
        return w

    def _page_changed(self, i: int):
        self.pages.setCurrentIndex(i)
        if i == 1:
            self.shelf.refresh(force=True)
        elif i == 2:
            self.watch.refresh_status()

    def _build_tray(self) -> QtWidgets.QWidget:
        self.tray = QtWidgets.QFrame()
        self.tray.setObjectName("tray")
        lay = QtWidgets.QVBoxLayout(self.tray)
        lay.setContentsMargins(22, 8, 22, 10)
        lay.setSpacing(6)

        head = QtWidgets.QHBoxLayout()
        head.setSpacing(14)
        t = QtWidgets.QLabel("Tray")
        t.setFont(UI.serif(12))
        head.addWidget(t)
        self.tab_dl = QtWidgets.QPushButton("Downloading 0")
        self.tab_fin = QtWidgets.QPushButton("Finished 0")
        grp = QtWidgets.QButtonGroup(self)
        grp.setExclusive(True)
        for b in (self.tab_dl, self.tab_fin):
            b.setObjectName("seg")
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            grp.addButton(b)
            head.addWidget(b)
        self.tab_dl.setChecked(True)
        grp.buttonClicked.connect(lambda *_: self._tab_changed())
        self.summary = QtWidgets.QLabel("")
        self.summary.setStyleSheet(f"color: {UI.MUTED};")
        head.addWidget(self.summary)
        head.addStretch(1)

        add = QtWidgets.QToolButton()
        add.setIcon(UI.icon("plus", UI.ACCENT, 18))
        add.setToolTip("Paste the link you copied and start downloading")
        add.clicked.connect(lambda: self._add_from_clipboard(True))
        more = QtWidgets.QToolButton()
        more.setIcon(UI.icon("chevron", UI.MUTED, 16))
        more.setToolTip("More ways to add")
        more.setPopupMode(QtWidgets.QToolButton.InstantPopup)
        m = QtWidgets.QMenu(more)
        m.addAction("Paste copied link(s) and start", lambda: self._add_from_clipboard(True))
        m.addAction("Add several links…", lambda: self._add_dialog(False))
        m.addAction("Record a live stream…", lambda: self._add_dialog(True))
        more.setMenu(m)
        head.addWidget(add)
        head.addWidget(more)
        b = QtWidgets.QPushButton("Resume all")
        b.setObjectName("outline")
        b.clicked.connect(self._resume_all)
        head.addWidget(b)
        b = QtWidgets.QPushButton("Pause all")
        b.setObjectName("outline")
        b.clicked.connect(self._pause_all)
        head.addWidget(b)
        trash = QtWidgets.QToolButton()
        trash.setIcon(UI.icon("trash", UI.MUTED, 18))
        trash.setToolTip("Remove selected (or everything in this list)")
        trash.clicked.connect(self._trash)
        head.addWidget(trash)
        self.collapse_btn = QtWidgets.QPushButton("Collapse ▾")
        self.collapse_btn.setObjectName("small")
        self.collapse_btn.clicked.connect(self._toggle_tray)
        head.addWidget(self.collapse_btn)
        lay.addLayout(head)

        self.tray_body = QtWidgets.QWidget()
        bl = QtWidgets.QVBoxLayout(self.tray_body)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.setSpacing(6)

        self.model = ListModel(self.mgr)
        self.proxy = FilterProxy()
        self.proxy.setSourceModel(self.model)
        self.view = TrayView()
        self.view.setModel(self.proxy)
        self.view.setViewMode(QtWidgets.QListView.IconMode)
        self.view.setResizeMode(QtWidgets.QListView.Adjust)
        self.view.setMovement(QtWidgets.QListView.Static)
        self.view.setSpacing(0)
        self.view.setMouseTracking(True)
        self.delegate = UI.CardDelegate(self.view, self.thumbs)
        self.delegate.action.connect(self._card_action)
        self.view.setItemDelegate(self.delegate)
        self.view.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        self.view.setVerticalScrollMode(QtWidgets.QAbstractItemView.ScrollPerPixel)
        self.view.setContextMenuPolicy(Qt.CustomContextMenu)
        self.view.customContextMenuRequested.connect(self._context_menu)
        self.view.doubleClicked.connect(lambda _: self._open_file())
        self.view.resized.connect(self._layout_cards)
        self.empty = UI.EmptyState("Nothing here yet", "Paste a link above, or just copy one anywhere.")
        self.list_stack = QtWidgets.QStackedWidget()
        self.list_stack.addWidget(self.empty)
        self.list_stack.addWidget(self.view)
        self.list_stack.setMinimumHeight(150)
        bl.addWidget(self.list_stack, 1)

        opts = QtWidgets.QHBoxLayout()
        opts.setSpacing(8)
        self.opt_btn = QtWidgets.QPushButton()
        self.opt_btn.setObjectName("small")
        self.opt_btn.setCursor(Qt.PointingHandCursor)
        self.opt_btn.setToolTip("Format, quality and subtitles")
        opts.addWidget(self.opt_btn)
        self.opt_more = QtWidgets.QWidget()
        mh = QtWidgets.QHBoxLayout(self.opt_more)
        mh.setContentsMargins(0, 0, 0, 0)
        mh.setSpacing(8)

        def combo(label, items, current, setter):
            lb = QtWidgets.QLabel(label)
            lb.setStyleSheet(f"color: {UI.MUTED};")
            mh.addWidget(lb)
            c = QtWidgets.QComboBox()
            c.addItems(items)
            c.setCurrentText(current)
            c.currentTextChanged.connect(setter)
            c.currentTextChanged.connect(lambda _t: self._opt_summary())
            mh.addWidget(c)
            return c

        combo("Format", ["MP4", "MKV", "WEBM", "MP3 (audio)"], self.cfg.fmt, lambda t: self._pref("fmt", t))
        combo("Quality", ["Best", "2160p", "1440p", "1080p", "720p", "480p", "360p"],
              self.cfg.quality, lambda t: self._pref("quality", t))
        combo("Subtitle", ["None"] + list(E.SUBTITLE_CODES), self.cfg.subtitle,
              lambda t: self._pref("subtitle", t))
        self.opt_more.hide()
        opts.addWidget(self.opt_more)
        self.opt_btn.clicked.connect(lambda: [self.opt_more.setVisible(not self.opt_more.isVisible()),
                                              self._opt_summary()])
        self._opt_summary()
        opts.addSpacing(10)
        lb = QtWidgets.QLabel("Save to")
        lb.setStyleSheet(f"color: {UI.MUTED};")
        opts.addWidget(lb)
        self.loc = QtWidgets.QLineEdit(self.cfg.download_dir)
        self.loc.setReadOnly(True)
        self.loc.setObjectName("path")
        opts.addWidget(self.loc, 1)
        b = QtWidgets.QPushButton("Change")
        b.setObjectName("small")
        b.clicked.connect(self._change_dir)
        opts.addWidget(b)
        fb = QtWidgets.QToolButton()
        fb.setIcon(UI.icon("folder", UI.MUTED, 18))
        fb.setToolTip("Open folder")
        fb.clicked.connect(self._open_dir)
        opts.addWidget(fb)
        bl.addLayout(opts)
        lay.addWidget(self.tray_body)
        return self.tray

    def _opt_summary(self):
        c = self.cfg
        sub = "" if c.subtitle in ("", "None") else " · " + c.subtitle
        arrow = "◂" if self.opt_more.isVisible() else "▾"
        self.opt_btn.setText(f"{c.fmt.split()[0]} · {c.quality}{sub}  {arrow}")

    def _toggle_tray(self):
        self.cfg.tray_collapsed = not self.cfg.tray_collapsed
        self.cfg.save()
        self._apply_tray_state()

    def _apply_tray_state(self):
        c = self.cfg.tray_collapsed
        self.tray_body.setVisible(not c)
        self.collapse_btn.setText("Expand ▴" if c else "Collapse ▾")
        QtCore.QTimer.singleShot(0, self._place_overlays)

    def _layout_cards(self):
        vw = max(300, self.view.viewport().width() - 2)
        cols = max(1, min(4, vw // 330))
        w = vw // cols
        self.delegate.cell = QtCore.QSize(w, 92)
        self.view.setGridSize(self.delegate.cell)

    def _place_overlays(self):
        top = self.tray.y()
        self.toast.reposition(top)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._place_overlays()

    # --- performance ------------------------------------------------------
    def _apply_perf(self):
        p = E.perf(self.cfg)
        self.timer.start(p["tick"])
        E.apply_priority(self.cfg)
        self.thumbs.set_workers(p["thumbs"])

    # --- helpers ----------------------------------------------------------
    def _selected(self) -> list[E.Item]:
        rows = {self.proxy.mapToSource(i).row() for i in self.view.selectionModel().selectedRows()}
        return [self.mgr.items[r] for r in sorted(rows) if r < len(self.mgr.items)]

    def _pref(self, key: str, value: str):
        setattr(self.cfg, key, value)
        self.cfg.save()

    def _tab_changed(self):
        if self.tab_fin.isChecked():
            self.proxy.pred = lambda i: i.status == "Completed"
        else:
            self.proxy.pred = lambda i: i.status != "Completed"
        self.proxy.invalidateFilter()

    def _tick(self):
        self.mgr.tick()
        self.model.refresh()
        self.proxy.invalidateFilter()
        self._tick_n += 1
        items = self.mgr.items
        n_fin = sum(1 for i in items if i.status == "Completed")
        n_dl = len(items) - n_fin
        t1, t2 = f"Downloading {n_dl}", f"Finished {n_fin}"
        if self.tab_dl.text() != t1:
            self.tab_dl.setText(t1)
        if self.tab_fin.text() != t2:
            self.tab_fin.setText(t2)
        self.list_stack.setCurrentIndex(1 if self.proxy.rowCount() else 0)
        self.empty.title.setText("Nothing finished yet" if self.tab_fin.isChecked() else "Nothing here yet")
        self.empty.sub.setText("Finished downloads and recordings land here." if self.tab_fin.isChecked()
                               else "Paste a link above, or just copy one anywhere.")
        act = self.mgr.active_count()
        spd = sum(i.speed for i in items if i.running)
        rec = sum(1 for i in items if i.status == "Recording")
        dls = sum(1 for i in items if i.status == "Downloading")
        bits = []
        if dls:
            bits.append(f"{dls} downloading" + (f" · {E.fmt_speed(spd)}" if spd else ""))
        if rec:
            bits.append(f"{rec} recording")
        txt = "  ·  ".join(bits)
        if self.summary.text() != txt:
            self.summary.setText(txt)
        self.mascot.set_mood("working" if act else "sleepy")
        title = f"Grabwave {VERSION}"
        if act:
            title += f"  —  {act} active"
        if self.windowTitle() != title:
            self.setWindowTitle(title)

        # celebrate what just finished; note what failed
        for it in items:
            if it.running or it.status in ("Downloading", "Recording", "Waiting", "Processing"):
                self._seen_running.add(it.id)
            elif it.id not in self._announced:
                if it.status == "Completed" and it.id in self._seen_running:
                    self._announced.add(it.id)
                    word = "recorded" if it.kind == "live" else "saved"
                    self.splash.play(f"{it.title}", f"is {word}. Find it on your shelf.", self.tray.y())
                    if self.cfg.sounds:
                        QtWidgets.QApplication.beep()
                elif it.status == "Failed":
                    self._announced.add(it.id)
                    if not self._auto_capture(it):
                        self.statusBar().showMessage("One didn't work. Right-click it in the tray for details.", 6000)
        if self.pages.currentIndex() == 1 and self._tick_n % 2 == 0:
            self.shelf.refresh()
        elif self.pages.currentIndex() == 2:
            self.watch.refresh_status()

    # --- automatic "open the page and capture the stream" fallback ----------------
    def _auto_capture(self, it: E.Item) -> bool:
        """When the normal download fails, quietly open the page, press play and record the stream it loads."""
        from urllib.parse import urlparse
        if not HAVE_WEB or it.kind != "download" or it.alt_url or it.id in self._auto_tried:
            return False
        if not it.url.lower().startswith(("http://", "https://")) or is_blocked_host(it.url):
            return False
        host = (urlparse(it.url).hostname or "").lower()
        if any(host == h or host.endswith("." + h) for h in NO_AUTO_CAPTURE):
            return False
        self._auto_tried.add(it.id)
        it.status = "Retrying"
        sn = PageSniffer(self.browser.profile, it.url, self)
        self._sniffers.append(sn)
        sn.finished.connect(lambda best, i=it, s=sn: self._on_capture(i, best, s))
        return True

    def _on_capture(self, it: E.Item, best: str, sn):
        if sn in self._sniffers:
            self._sniffers.remove(sn)
        sn.deleteLater()
        if best:
            it.alt_url, it.alt_referer = best, it.url
            it.error = ""
            it.status = "Queued"
            self.statusBar().showMessage("Found the video on the page. Trying again…", 5000)
        else:
            it.status = "Failed"
            it.error += ("\n\nGrabwave also opened the page itself and found no video stream. If the video needs "
                         "a sign-in or a click, open it in the Browse tab, start it, then click the download button.")
            self.statusBar().showMessage("Couldn't find a video on that page.", 6000)

    # --- clipboard / smart paste -------------------------------------------
    def _clipboard_urls(self) -> list:
        import re
        text = QtWidgets.QApplication.clipboard().text() or ""
        urls = [u.rstrip(".,;)'\"") for u in re.findall(r"https?://[^\s<>\"']+", text)]
        return list(dict.fromkeys(urls))

    def _clip_changed(self):
        urls = self._clipboard_urls()
        url = urls[0] if urls else ""
        self.browser.home.set_clipboard_url(url)
        if not url or not self.cfg.smart_paste or url == self._last_offer:
            return
        if any(i.url == url for i in self.mgr.items):
            return
        self._last_offer = url
        self.toast.offer(url)
        self._place_overlays()

    def _add_from_clipboard(self, ask_if_empty: bool = False):
        """Add whatever link(s) are on the clipboard and start right away (no window)."""
        urls = self._clipboard_urls()
        if not urls:
            if ask_if_empty:
                self._add_dialog(False)  # nothing copied yet: let the person type or paste a link
            else:
                self.statusBar().showMessage("No link on the clipboard", 2500)
            return
        for u in urls:
            self._add_url(u, announce=False)
        self.statusBar().showMessage(f"Added {len(urls)} link" + ("" if len(urls) == 1 else "s") + " and started", 3000)

    def _add_url(self, u: str, quality: str = "", announce: bool = True) -> E.Item:
        here = self.browser.current_url().rstrip("/")
        kind = "live" if E.cam_detect(u) else "download"
        it = self.mgr.add(u, kind, "")
        if quality:
            it.quality = quality
        if kind == "download" and u.rstrip("/") == here:  # the page open in the built-in browser
            alt = self.browser.best_media()
            if alt:
                it.alt_url, it.alt_referer = alt, u
        self.model.reset()
        self.mgr.save()
        self.tab_dl.setChecked(True)
        self._tab_changed()
        if announce:
            self.statusBar().showMessage("Added and started", 3000)
        return it

    def keyPressEvent(self, e):
        if e.matches(QtGui.QKeySequence.Paste) and not isinstance(
                QtWidgets.QApplication.focusWidget(), (QtWidgets.QLineEdit, QtWidgets.QPlainTextEdit)):
            self._add_from_clipboard(False)  # Ctrl+V anywhere outside a text box
            e.accept()
            return
        super().keyPressEvent(e)

    # --- adding ---------------------------------------------------------------
    def _add_dialog(self, live: bool):
        d = AddDialog(self, self.cfg, live)
        if d.exec() == QtWidgets.QDialog.Accepted:
            self._add_urls(d.urls(), d.kind())

    def _add_from_browser(self, url: str):
        kind = "live" if E.cam_detect(url) else "download"
        self._add_urls([url], kind)
        self.statusBar().showMessage("Added to downloads", 2500)

    def _add_media(self, info: dict):
        """Called when the page's Download button is clicked in the built-in browser."""
        import re
        media = (info.get("media") or "").strip()
        page = (info.get("page") or "").strip()
        url = media if media.lower().startswith(("http://", "https://")) else page
        if not url:
            return
        it = self.mgr.add(url, "live" if E.cam_detect(url) else "download", "")
        referer = (info.get("referer") or "").strip()
        if referer and referer != url and media:  # direct media address: send the page that played it as Referer
            it.referer = referer
        alt = (info.get("alt") or "").strip()
        emb = (info.get("embed") or "").strip()
        if alt.lower().startswith(("http://", "https://")) and alt != url:
            it.alt_url = alt
            it.alt_referer = (info.get("alt_referer") or page or "").strip()
        elif emb.lower().startswith(("http://", "https://")) and emb != url:
            it.alt_url = emb  # the player frame's own address, tried if the page address fails
            it.alt_referer = (info.get("embedReferer") or page or "").strip()
        if media or it.alt_url:  # direct address or captured stream: use the page title as the file name
            name = re.sub(r'[\\/:*?"<>|%\r\n\t]', "", (info.get("title") or "")).strip()[:150].strip()
            if name:
                it.filename = name
                it.title = name
        self.model.reset()
        self.mgr.save()
        self.tab_dl.setChecked(True)
        self._tab_changed()
        self.statusBar().showMessage("Added to downloads", 2500)

    def _add_urls(self, urls, kind="download"):
        for u in urls:
            self.mgr.add(u, kind, self.cfg.live_dir if kind == "live" else "")
        if urls:
            self.model.reset()
            self.mgr.save()
            self.tab_dl.setChecked(True)
            self._tab_changed()

    # --- card / list actions ----------------------------------------------------
    def _card_action(self, it: E.Item, which: str):
        if which == "remove":
            self._remove([it], False)
        elif which == "folder":
            if it.path and os.path.exists(it.path):
                open_path(str(Path(it.path).parent))
            else:
                self._open_dir()
        elif it.status == "Failed":
            # "Fix it": go through every route again, including the automatic page capture
            self._announced.discard(it.id)
            self._auto_tried.discard(it.id)
            it.alt_url = ""
            self.mgr.resume(it)
            self.statusBar().showMessage("Trying every way we know…", 5000)
        elif it.status in ("Paused", "Stopped"):
            self.mgr.resume(it)
        elif it.status == "Completed":
            if it.path and os.path.exists(it.path):
                self._play(it.path, it.title)
        else:
            self.mgr.request_stop(it, "pause")

    def _resume_all(self):
        for it in self.mgr.items:
            self.mgr.resume(it)

    def _pause_all(self):
        for it in self.mgr.items:
            self.mgr.request_stop(it, "pause")

    def _trash(self):
        sel = self._selected()
        if not sel:
            rows = [self.mgr.items[self.proxy.mapToSource(self.proxy.index(r, 0)).row()]
                    for r in range(self.proxy.rowCount())]
            if not rows:
                return
            if QtWidgets.QMessageBox.question(
                    self, "Remove", f"Remove all {len(rows)} item(s) from this list?\n(Files are kept.)"
            ) != QtWidgets.QMessageBox.Yes:
                return
            sel = rows
        self._remove(sel, False)

    def _remove(self, items, delete_file: bool):
        for it in items:
            self.mgr.remove(it, delete_file)
            self.thumbs.forget(it.id)
            if delete_file:
                try:
                    E.thumb_path(it).unlink(missing_ok=True)
                except OSError:
                    pass
        self.model.reset()
        self.mgr.save()
        self.shelf.refresh(force=True)

    def _open_file(self):
        sel = self._selected()
        if sel and sel[0].path and os.path.exists(sel[0].path):
            open_path(sel[0].path)

    def _open_item_folder(self):
        sel = self._selected()
        if sel and sel[0].path and os.path.exists(sel[0].path):
            open_path(str(Path(sel[0].path).parent))
        else:
            self._open_dir()

    def _open_dir(self):
        d = self.cfg.download_dir
        Path(d).mkdir(parents=True, exist_ok=True)
        open_path(d)

    def _change_dir(self):
        d = QtWidgets.QFileDialog.getExistingDirectory(self, "Save downloads to", self.cfg.download_dir)
        if d:
            old = self.cfg.download_dir
            if self.cfg.live_dir.startswith(old):
                self.cfg.live_dir = str(Path(d) / "Recordings")
            self.cfg.download_dir = d
            self.cfg.save()
            self.loc.setText(d)

    def _settings(self):
        if SettingsDialog(self, self.cfg).exec() == QtWidgets.QDialog.Accepted:
            self._apply_perf()
            self.statusBar().showMessage(
                f"Performance: {self.cfg.performance}. The browser part changes after Grabwave restarts.", 6000)

    # --- shelf actions ------------------------------------------------------------
    def _shelf_open(self, it: E.Item):
        if it.path and os.path.exists(it.path):
            self._play(it.path, it.title)

    def _play(self, path: str, title: str = ""):
        """Play in Grabwave's own player (it can play H.264, which the built-in browser can't); if Qt's media
        player isn't available, hand the file to the computer's default video player instead."""
        try:
            dlg = UI.PlayerDialog(path, title, None)
        except Exception:  # noqa: BLE001
            open_path(path)
            return
        self._players = [p for p in getattr(self, "_players", []) if p.isVisible()] + [dlg]
        dlg.show()

    def _shelf_folder(self, it: E.Item):
        if it.path and os.path.exists(it.path):
            open_path(str(Path(it.path).parent))

    def _shelf_convert(self, it: E.Item, kind: str):
        self._run_tool(f"Converting to {kind.upper()}…", it, lambda: E.convert_file(it.path, kind, self.cfg))

    def _shelf_trim(self, it: E.Item):
        d = UI.TrimDialog(self, it.title)
        if d.exec() != QtWidgets.QDialog.Accepted:
            return
        start, end = d.values()
        if not start and not end:
            return
        self._run_tool("Trimming…", it, lambda: E.trim_file(it.path, start, end))

    def _run_tool(self, label: str, src: E.Item, fn):
        import threading
        self.statusBar().showMessage(f"{label} {src.title}")

        def work():
            try:
                self.toolFinished.emit(src, fn(), "")
            except Exception as e:  # noqa: BLE001
                self.toolFinished.emit(src, "", str(e))

        threading.Thread(target=work, daemon=True).start()

    def _on_tool(self, src: E.Item, out: str, err: str):
        self.statusBar().clearMessage()
        if err:
            QtWidgets.QMessageBox.warning(self, "That didn't work", err)
            return
        new = E.Item(src.url, src.kind)
        new.title = Path(out).stem
        new.status = "Completed"
        new.path = out
        new.done = os.path.getsize(out)
        new.added = time.time()
        new.duration = src.duration
        self.mgr.items.append(new)
        self._announced.add(new.id)
        self.model.reset()
        self.mgr.save()
        self.shelf.refresh(force=True)
        self.splash.play(new.title, "is on your shelf.", self.tray.y())

    # --- video engine (yt-dlp) kept fresh quietly; used the next time Grabwave starts -------
    def _auto_engine(self):
        import threading
        import time as _t
        if _t.time() - self.cfg.engine_checked < 20 * 3600:
            return
        self.cfg.engine_checked = _t.time()
        self.cfg.save()

        def work():
            try:
                E.update_ytdlp("nightly", self.cfg.proxy)
            except Exception:  # noqa: BLE001 - offline or blocked: just try again tomorrow
                pass

        threading.Thread(target=work, daemon=True).start()

    # --- updates ---------------------------------------------------------------
    def _check_updates(self, manual: bool):
        import threading

        def work():
            try:
                self.updateChecked.emit(U.check(VERSION, self.cfg.update_url, self.cfg.proxy), "", manual)
            except Exception as e:  # noqa: BLE001
                self.updateChecked.emit(None, str(e), manual)

        if manual:
            self.statusBar().showMessage("Checking for updates…", 4000)
        threading.Thread(target=work, daemon=True).start()

    def _on_update_checked(self, entry, err: str, manual: bool):
        if err:
            if manual:
                QtWidgets.QMessageBox.information(self, "Check for updates",
                                                  f"Couldn't check for updates right now.\n\n{err}")
            return
        if entry is None:
            if manual:
                QtWidgets.QMessageBox.information(self, "Check for updates",
                                                  f"You're up to date. Grabwave {VERSION} is the newest version.")
            return
        if self._update_open or not self.isVisible():
            return
        if not manual and (QtWidgets.QApplication.activeModalWidget() is not None or not self.isActiveWindow()):
            # another window is open (or Grabwave is in the background): ask again in a few seconds
            QtCore.QTimer.singleShot(6000, lambda e=entry: self._on_update_checked(e, "", False))
            return
        self._update_open = True
        dlg = UI.UpdateDialog(self, entry, VERSION, self.cfg.proxy, self.mgr.active_count())
        dlg.quitRequested.connect(self._quit_app)
        dlg.exec()
        self._update_open = False

    def _guide(self):
        QtWidgets.QMessageBox.information(
            self, "Quick guide",
            "<b>Download a video</b><br>Paste a link in the box on the Browse page and press Enter, "
            "or open a site in the built-in browser and press the red download button.<br><br>"
            "<b>Record a live stream</b><br>Paste the room or stream link. Recording starts and keeps "
            "going until the stream ends or you stop it.<br><br>"
            "<b>Shelf</b> shows everything you've saved. Right-click a video to convert or trim it.<br><br>"
            "<b>Live</b> is your Watchlist. Grabwave starts recording when a room goes live.<br><br>"
            "<b>Updates</b>: Help, then Check for updates. Grabwave also checks when it opens.")

    def _about(self):
        try:
            import yt_dlp
            engine = f"yt-dlp {yt_dlp.version.__version__}"
        except Exception:
            engine = "yt-dlp not found"
        QtWidgets.QMessageBox.about(
            self, "About Grabwave",
            f"<h3>Grabwave {VERSION}</h3><p>Download videos and record live streams.</p>"
            f"<p style='color:#6b6458'>{engine}<br>ffmpeg: {E.find_ffmpeg() or 'not found'}</p>"
            "<p style='color:#6b6458'>Built with Qt, yt-dlp, streamlink and ffmpeg. "
            "See THIRD_PARTY.md for their licenses.</p>")

    # --- watchlist / tray icon ----------------------------------------------------
    def _went_live(self, name: str):
        msg = f"{name} just went live. Recording started."
        if self.tray_icon is not None and self.tray_icon.isVisible():
            self.tray_icon.showMessage("Grabwave", msg, QtWidgets.QSystemTrayIcon.Information, 6000)
        self.statusBar().showMessage(msg, 8000)
        self.model.reset()
        self.mgr.save()

    def _show_from_tray(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _quit_app(self):
        self._quitting = True
        self.close()
        # make sure no helper process of the browser engine is left holding files (an update needs them free)
        QtCore.QTimer.singleShot(4000, lambda: os._exit(0))

    def _show_details(self, it: E.Item):
        try:
            import yt_dlp
            ver = f"yt-dlp {yt_dlp.version.__version__}"
        except Exception:
            ver = "yt-dlp missing"
        try:
            import streamlink
            ver += f", streamlink {streamlink.__version__}"
        except Exception:
            ver += ", streamlink missing"
        try:
            import curl_cffi
            ver += f", curl_cffi {curl_cffi.__version__}"
        except Exception:
            ver += ", curl_cffi NOT installed"
        dlg = QtWidgets.QDialog(self)
        dlg.setWindowTitle("Details")
        dlg.resize(720, 360)
        lay = QtWidgets.QVBoxLayout(dlg)
        t = QtWidgets.QPlainTextEdit(f"URL: {it.url}\nStatus: {it.status}\n{ver}\n\n{it.error or '(no error)'}")
        t.setReadOnly(True)
        lay.addWidget(t)
        b = QtWidgets.QPushButton("Copy")
        b.clicked.connect(lambda: QtWidgets.QApplication.clipboard().setText(t.toPlainText()))
        lay.addWidget(b)
        dlg.exec()

    def _context_menu(self, pos):
        sel = self._selected()
        if not sel:
            return
        m = QtWidgets.QMenu(self)
        m.addAction("Resume", lambda: [self.mgr.resume(i) for i in sel])
        m.addAction("Pause / stop", lambda: [self.mgr.request_stop(i, "pause") for i in sel])
        m.addAction("Restart from scratch", lambda: [self.mgr.restart(i) for i in sel])
        m.addSeparator()
        m.addAction("Open file", self._open_file)
        m.addAction("Open folder", self._open_item_folder)
        m.addAction("Copy URL", lambda: QtWidgets.QApplication.clipboard().setText("\n".join(i.url for i in sel)))
        m.addAction("Show error / details…", lambda: self._show_details(sel[0]))
        m.addSeparator()
        m.addAction("Remove from list", lambda: self._remove(sel, False))
        m.addAction("Remove and delete file", lambda: self._remove(sel, True))
        m.exec(self.view.viewport().mapToGlobal(pos))

    # --- drag & drop ------------------------------------------------------
    def dragEnterEvent(self, e):
        if e.mimeData().hasText() or e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        urls = [l.strip() for l in e.mimeData().text().splitlines()
                if l.strip().lower().startswith(("http://", "https://"))]
        self._add_urls(urls, "download")

    def closeEvent(self, e):
        if (not self._quitting and self.tray_icon is not None and self.cfg.close_to_tray
                and self.watcher.enabled_count() > 0):
            e.ignore()
            self.hide()
            if not self._tray_hinted:
                self._tray_hinted = True
                self.tray_icon.showMessage(
                    "Grabwave is still running",
                    f"Watching {self.watcher.enabled_count()} room(s). Right-click the tray icon to quit.",
                    QtWidgets.QSystemTrayIcon.Information, 6000)
            return
        self.timer.stop()
        self.mgr.shutdown()
        if self.tray_icon is not None:
            self.tray_icon.hide()
        e.accept()


def main():
    print(f"Grabwave {VERSION} running from {Path(__file__).resolve()}")
    # Qt 6.10+ marks invalidateFilter() deprecated; it still works and the notice only adds noise
    warnings.filterwarnings("ignore", message=".*invalidateFilter.*", category=DeprecationWarning)
    E.activate_ytdlp_update()  # use a newer downloaded yt-dlp if the user updated it (must run before any import)
    cfg = E.Config.load()
    E.apply_priority(cfg)
    flags = E.perf(cfg)["chromium"]  # browser-engine flags for the chosen performance mode (before Qt starts)
    if flags:
        os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = (os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS", "") + " " + flags).strip()
    if os.name == "nt":
        try:  # lets Windows show Grabwave's own icon on the taskbar instead of Python's
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Grabwave.App")
        except Exception:
            pass
    app = QtWidgets.QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setApplicationName("Grabwave")
    icon_file = resource_path("assets/grabwave.png")
    if icon_file.is_file():
        app.setWindowIcon(QtGui.QIcon(str(icon_file)))
    app.setStyleSheet(UI.build_style())
    mgr = E.Manager(cfg)
    w = MainWindow(mgr)
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
