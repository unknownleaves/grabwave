"""Grabwave GUI.

Layout modelled on MeGet: a built-in browser (with site shortcuts) on the left and a
card-style downloads panel on the right (Downloading / Finished tabs, Format / Quality /
Subtitle selectors and a file-location row at the bottom).
"""
from __future__ import annotations

import os
import subprocess
import warnings
import sys
import time
from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets
from PySide6.QtCore import Qt

import engine as E

VERSION = "1.8"
import camsites

try:  # the built-in browser needs Qt WebEngine (shipped with the PySide6 wheels)
    from PySide6.QtWebEngineCore import QWebEngineProfile, QWebEnginePage, QWebEngineScript
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

ACCENT = "#5b3df5"
RED = "#e53935"
GRAY = "#8a8a8a"

DEFAULT_SITES = [
    ("YouTube", "https://www.youtube.com", "#e62117"),
    ("Vimeo", "https://vimeo.com", "#1ab7ea"),
    ("Facebook", "https://www.facebook.com", "#1877f2"),
    ("Instagram", "https://www.instagram.com", "#c13584"),
    ("TikTok", "https://www.tiktok.com", "#161616"),
    ("Twitch", "https://www.twitch.tv", "#9146ff"),
    ("Dailymotion", "https://www.dailymotion.com", "#0066dc"),
    ("Bilibili", "https://www.bilibili.com", "#00a1d6"),
    ("SoundCloud", "https://soundcloud.com", "#ff5500"),
    ("Stripchat", "https://stripchat.com", "#6c4cf5"),
    ("Chaturbate", "https://chaturbate.com", "#f6922d"),
]

STYLE = f"""
QWidget {{ font-family: "Segoe UI", "Noto Sans", "Ubuntu", sans-serif; font-size: 13px; color: #222; }}
QMainWindow, QSplitter {{ background: #ffffff; }}
#left {{ background: #ffffff; }}
#right {{ background: #f5f5f5; }}
QToolButton {{ border: none; background: transparent; padding: 4px 8px; border-radius: 4px; font-size: 18px; }}
QToolButton:hover {{ background: #e9e9e9; }}
QToolButton:disabled {{ color: #bbb; }}
QToolButton::menu-indicator {{ image: none; }}
QLineEdit {{ border: 1px solid #dcdcdc; border-radius: 4px; padding: 5px 8px; background: #ffffff; }}
QLineEdit:focus {{ border: 1px solid {ACCENT}; }}
QLineEdit[readOnly="true"] {{ background: #ececec; color: #666; }}
QPushButton#primary {{ background: {ACCENT}; color: white; border: none; border-radius: 4px; padding: 6px 16px; }}
QPushButton#primary:hover {{ background: #4a2fe0; }}
QPushButton#outline {{ background: #ffffff; border: 1px solid #cfcfcf; border-radius: 4px; padding: 5px 14px; }}
QPushButton#outline:hover {{ background: #f0f0f0; }}
QPushButton#small {{ background: #ffffff; border: 1px solid #cfcfcf; border-radius: 3px; padding: 2px 8px; }}
#segwrap {{ background: #e3e3e3; border-radius: 6px; }}
#segwrap QPushButton {{ border: none; background: transparent; padding: 5px 0; border-radius: 5px; min-width: 112px; }}
#segwrap QPushButton:checked {{ background: #ffffff; }}
QComboBox {{ border: 1px solid #cfcfcf; border-radius: 4px; padding: 3px 8px; background: #efefef; min-width: 64px; }}
QComboBox QAbstractItemView {{ background: white; selection-background-color: {ACCENT}; selection-color: white; }}
QListView {{ background: transparent; border: none; outline: 0; }}
QFrame#sep {{ background: #e0e0e0; max-height: 1px; min-height: 1px; }}
"""


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

class Tile(QtWidgets.QToolButton):
    def __init__(self, label: str, color: str, glyph: str | None = None):
        super().__init__()
        pm = QtGui.QPixmap(48, 48)
        pm.fill(Qt.transparent)
        p = QtGui.QPainter(pm)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(QtGui.QColor(color))
        p.drawEllipse(0, 0, 48, 48)
        p.setPen(QtGui.QColor("white"))
        f = p.font()
        f.setBold(True)
        f.setPointSize(16)
        p.setFont(f)
        p.drawText(pm.rect(), Qt.AlignCenter, glyph or label[:1].upper())
        p.end()
        self.setIcon(QtGui.QIcon(pm))
        self.setIconSize(QtCore.QSize(48, 48))
        self.setText(label)
        self.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
        self.setFixedSize(124, 96)
        self.setCursor(Qt.PointingHandCursor)
        self.setStyleSheet("QToolButton { font-size: 12px; }")


class HomePage(QtWidgets.QWidget):
    siteClicked = QtCore.Signal(str)

    def __init__(self, cfg: E.Config):
        super().__init__()
        self.cfg = cfg
        outer = QtWidgets.QVBoxLayout(self)
        outer.addStretch(3)
        wrap = QtWidgets.QWidget()
        self.grid = QtWidgets.QGridLayout(wrap)
        self.grid.setHorizontalSpacing(4)
        self.grid.setVerticalSpacing(10)
        outer.addWidget(wrap, 0, Qt.AlignHCenter)
        outer.addStretch(2)
        self.rebuild()

    def rebuild(self):
        while self.grid.count():
            w = self.grid.takeAt(0).widget()
            if w:
                w.deleteLater()
        sites = list(DEFAULT_SITES) + [(n, u, "#7a7a7a") for n, u in self.cfg.custom_sites]
        for i, (name, url, color) in enumerate(sites):
            t = Tile(name, color)
            t.clicked.connect(lambda _=False, u=url: self.siteClicked.emit(u))
            self.grid.addWidget(t, i // 4, i % 4)
        plus = Tile("", "#bdbdbd", "+")
        plus.setToolTip("Add a shortcut")
        plus.clicked.connect(self._add_site)
        n = len(sites)
        self.grid.addWidget(plus, n // 4, n % 4)

    def _add_site(self):
        name, ok = QtWidgets.QInputDialog.getText(self, "Add shortcut", "Name:")
        if not ok or not name.strip():
            return
        url, ok = QtWidgets.QInputDialog.getText(self, "Add shortcut", "Address:", text="https://")
        if not ok or not url.strip():
            return
        self.cfg.custom_sites.append([name.strip(), url.strip()])
        self.cfg.save()
        self.rebuild()


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
        bar.setContentsMargins(8, 8, 8, 8)
        bar.setSpacing(2)

        def btn(glyph, tip, slot):
            b = QtWidgets.QToolButton()
            b.setText(glyph)
            b.setToolTip(tip)
            b.clicked.connect(slot)
            bar.addWidget(b)
            return b

        self.b_home = btn("⌂", "Home", self.go_home)
        self.b_back = btn("←", "Back", lambda: self.web and self.web.back())
        self.b_fwd = btn("→", "Forward", lambda: self.web and self.web.forward())
        self.b_reload = btn("⟳", "Reload", lambda: self.web and self.web.reload())
        self.url = QtWidgets.QLineEdit()
        self.url.setPlaceholderText("Search or enter a web address")
        self.url.returnPressed.connect(lambda: self.navigate(self.url.text()))
        bar.addWidget(self.url, 1)
        self.b_add = btn("⬇", "Add this page to downloads", self._emit_add)
        self.b_ext = btn("↗", "Open in your default browser", self._open_external)
        lay.addLayout(bar)

        self.stack = QtWidgets.QStackedWidget()
        self.home = HomePage(cfg)
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
    _SKIP = ("preview", "thumb", "trailer", "sprite", "teaser", "poster", "doubleclick", "adserver", "/ads/")

    def _on_sniff(self, url: str, first_party: str):
        low = url.lower()
        if is_blocked_host(first_party) or any(w in low for w in self._SKIP):
            return
        if url not in self.sniffed:
            self.sniffed.append(url)
            del self.sniffed[:-40]

    def best_media(self) -> str:
        """The stream address most likely to be the video: a master playlist, else the first playlist, else the
        latest direct file."""
        if self.web is None:
            return ""
        def ext(u): return u.lower().split("?")[0]
        pls = [u for u in self.sniffed if ext(u).endswith(".m3u8")]
        if pls:
            masters = [u for u in pls if "master" in u.lower()]
            return (masters or pls)[0]
        mpd = [u for u in self.sniffed if ext(u).endswith(".mpd")]
        if mpd:
            return mpd[0]
        return self.sniffed[-1] if self.sniffed else ""

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


def status_line(it: E.Item) -> tuple[str, str]:
    s = it.status
    pct = min(100, int(it.done * 100 / it.total)) if it.total else None
    if s == "Failed":
        return "Failed to download. Please try again.", RED
    if s == "Downloading":
        parts = [f"{pct}%" if pct is not None else E.fmt_size(it.done)]
        if it.speed:
            parts.append(E.fmt_speed(it.speed))
        if it.eta:
            parts.append("ETA " + E.fmt_dur(it.eta))
        return "  ·  ".join(parts), GRAY
    if s == "Recording":
        parts = ["● Recording"]
        if it.started:
            parts.append(E.fmt_dur(time.time() - it.started))
        parts.append(E.fmt_size(it.done))
        if it.speed:
            parts.append(E.fmt_speed(it.speed))
        return "  ·  ".join(parts), RED
    if s == "Waiting":
        return "Waiting for the stream to start…", GRAY
    if s == "Processing":
        return "Converting…", GRAY
    if s == "Completed":
        return f"Completed  ·  {E.fmt_size(it.done or it.total)}", "#2e7d32"
    if s == "Paused":
        return "Paused" + (f"  ·  {pct}%" if pct is not None else ""), GRAY
    return s, GRAY


class CardDelegate(QtWidgets.QStyledItemDelegate):
    actionClicked = QtCore.Signal(object)
    ROW_H = 84

    @staticmethod
    def _geom(rect: QtCore.QRect):
        card = rect.adjusted(6, 4, -6, -4)
        thumb = QtCore.QRect(card.left() + 8, card.top() + 8, 92, card.height() - 16)
        action = QtCore.QRect(card.right() - 40, card.center().y() - 16, 32, 32)
        return card, thumb, action

    def sizeHint(self, option, index):
        return QtCore.QSize(option.rect.width(), self.ROW_H)

    def paint(self, p, opt, idx):
        it = idx.data(Qt.UserRole)
        if it is None:
            return
        card, thumb, action = self._geom(opt.rect)
        p.save()
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        selected = bool(opt.state & QtWidgets.QStyle.State_Selected)
        p.setPen(QtGui.QPen(QtGui.QColor(ACCENT if selected else "#e4e4e4")))
        p.setBrush(QtGui.QColor("#f1eeff" if selected else "#ffffff"))
        p.drawRoundedRect(card, 6, 6)

        # thumbnail placeholder
        p.setPen(Qt.NoPen)
        p.setBrush(QtGui.QColor("#d6d6d6"))
        p.drawRoundedRect(thumb, 4, 4)
        live = it.kind == "live"
        p.setPen(QtGui.QColor(RED if live and it.status == "Recording" else "#ffffff"))
        f = p.font()
        f.setPointSize(11 if live else 18)
        f.setBold(True)
        p.setFont(f)
        p.drawText(thumb, Qt.AlignCenter, "● LIVE" if live else "▶")

        # text
        x = thumb.right() + 12
        w = max(40, action.left() - 8 - x)
        f = p.font()
        f.setBold(False)
        f.setPointSize(10)
        p.setFont(f)
        fm = QtGui.QFontMetrics(f)
        p.setPen(QtGui.QColor("#222222"))
        title_rect = QtCore.QRect(x, card.top() + 9, w, 18)
        p.drawText(title_rect, Qt.AlignVCenter | Qt.AlignLeft, fm.elidedText(it.title, Qt.ElideMiddle, w))
        text, color = status_line(it)
        f.setPointSize(9)
        p.setFont(f)
        p.setPen(QtGui.QColor(color))
        fm2 = QtGui.QFontMetrics(f)
        p.drawText(QtCore.QRect(x, card.top() + 31, w, 18), Qt.AlignVCenter | Qt.AlignLeft,
                   fm2.elidedText(text, Qt.ElideRight, w))
        if it.status == "Downloading" and it.total:
            bar = QtCore.QRect(x, card.bottom() - 17, w, 6)
            p.setPen(Qt.NoPen)
            p.setBrush(QtGui.QColor("#e3e3e3"))
            p.drawRoundedRect(bar, 3, 3)
            fill = QtCore.QRect(bar)
            fill.setWidth(int(bar.width() * min(1.0, it.done / it.total)))
            p.setBrush(QtGui.QColor(ACCENT))
            p.drawRoundedRect(fill, 3, 3)

        # action glyph
        glyph, gcolor = self.action_glyph(it)
        f = p.font()
        f.setPointSize(13)
        p.setFont(f)
        p.setPen(QtGui.QColor(gcolor))
        p.drawText(action, Qt.AlignCenter, glyph)
        p.restore()

    @staticmethod
    def action_glyph(it: E.Item) -> tuple[str, str]:
        if it.status in ("Failed", "Paused", "Stopped"):
            return "⟳", RED if it.status == "Failed" else ACCENT
        if it.status == "Completed":
            return "↗", ACCENT
        if it.kind == "live" and it.running:
            return "■", RED
        return "❚❚", ACCENT

    def editorEvent(self, event, model, option, index):
        if event.type() == QtCore.QEvent.MouseButtonRelease and event.button() == Qt.LeftButton:
            _, _, action = self._geom(option.rect)
            if action.adjusted(-6, -6, 6, 6).contains(event.position().toPoint()):
                it = index.data(Qt.UserRole)
                if it is not None:
                    self.actionClicked.emit(it)
                return True
        return False


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
        self.resize(560, 0)
        form = QtWidgets.QFormLayout(self)

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
        form.addRow(f"Simultaneous downloads / recordings (max {E.MAX_CONCURRENT}):", self.maxc)

        self.quality = QtWidgets.QLineEdit(cfg.live_quality)
        self.quality.setPlaceholderText("best, worst, 720p, 1080p60 …")
        form.addRow("Live quality:", self.quality)

        self.poll = QtWidgets.QSpinBox()
        self.poll.setRange(5, 600)
        self.poll.setSuffix(" s")
        self.poll.setValue(cfg.live_poll_seconds)
        form.addRow("Check offline channels every:", self.poll)

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

class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, mgr: E.Manager):
        super().__init__()
        self.mgr = mgr
        self.cfg = mgr.cfg
        self.setWindowTitle(f"Grabwave {VERSION}")
        self.resize(1160, 820)
        self.setAcceptDrops(True)

        split = QtWidgets.QSplitter(Qt.Horizontal)
        split.setChildrenCollapsible(False)
        self.browser = BrowserPane(self.cfg)
        self.browser.addRequested.connect(self._add_from_browser)
        self.browser.mediaFound.connect(self._add_media)
        split.addWidget(self.browser)
        split.addWidget(self._build_right())
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        split.setSizes([660, 500])
        self.setCentralWidget(split)

        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(500)
        self.saver = QtCore.QTimer(self)
        self.saver.timeout.connect(mgr.save)
        self.saver.start(10000)
        self._tab_changed()
        self._tick()

    # --- right panel ------------------------------------------------------
    def _build_right(self) -> QtWidgets.QWidget:
        right = QtWidgets.QWidget()
        right.setObjectName("right")
        right.setAttribute(Qt.WA_StyledBackground, True)
        lay = QtWidgets.QVBoxLayout(right)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        # top row: + | Downloading / Finished | settings
        top = QtWidgets.QHBoxLayout()
        top.setContentsMargins(14, 10, 14, 8)
        add = QtWidgets.QToolButton()
        add.setText("+")
        add.setToolTip("Paste the link you copied and start downloading")
        add.clicked.connect(lambda: self._add_from_clipboard(True))
        more = QtWidgets.QToolButton()
        more.setText("▾")
        more.setToolTip("More ways to add")
        more.setPopupMode(QtWidgets.QToolButton.InstantPopup)
        m = QtWidgets.QMenu(more)
        m.addAction("Paste copied link(s) and start", lambda: self._add_from_clipboard(True))
        m.addAction("Add several links…", lambda: self._add_dialog(False))
        m.addAction("Record a live stream…", lambda: self._add_dialog(True))
        more.setMenu(m)
        top.addWidget(add)
        top.addWidget(more)
        top.addStretch(1)
        seg = QtWidgets.QFrame()
        seg.setObjectName("segwrap")
        sl = QtWidgets.QHBoxLayout(seg)
        sl.setContentsMargins(3, 3, 3, 3)
        sl.setSpacing(0)
        self.tab_dl = QtWidgets.QPushButton("Downloading")
        self.tab_fin = QtWidgets.QPushButton("Finished")
        grp = QtWidgets.QButtonGroup(self)
        grp.setExclusive(True)
        for b in (self.tab_dl, self.tab_fin):
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            grp.addButton(b)
            sl.addWidget(b)
        self.tab_dl.setChecked(True)
        grp.buttonClicked.connect(lambda *_: self._tab_changed())
        top.addWidget(seg)
        top.addStretch(1)
        gear = QtWidgets.QToolButton()
        gear.setText("⚙")
        gear.setToolTip("Settings")
        gear.clicked.connect(self._settings)
        top.addWidget(gear)
        lay.addLayout(top)

        # list
        self.model = ListModel(self.mgr)
        self.proxy = FilterProxy()
        self.proxy.setSourceModel(self.model)
        self.view = QtWidgets.QListView()
        self.view.setModel(self.proxy)
        self.delegate = CardDelegate(self.view)
        self.delegate.actionClicked.connect(self._card_action)
        self.view.setItemDelegate(self.delegate)
        self.view.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        self.view.setVerticalScrollMode(QtWidgets.QAbstractItemView.ScrollPerPixel)
        self.view.setUniformItemSizes(True)
        self.view.setContextMenuPolicy(Qt.CustomContextMenu)
        self.view.customContextMenuRequested.connect(self._context_menu)
        self.view.doubleClicked.connect(lambda _: self._open_file())
        lay.addWidget(self.view, 1)

        sep = QtWidgets.QFrame()
        sep.setObjectName("sep")
        lay.addWidget(sep)

        # footer row 1: count + Resume All / Pause All / trash
        f1 = QtWidgets.QHBoxLayout()
        f1.setContentsMargins(14, 10, 14, 6)
        self.count = QtWidgets.QLabel("0 items")
        f1.addWidget(self.count)
        f1.addStretch(1)
        b = QtWidgets.QPushButton("Resume All")
        b.setObjectName("outline")
        b.clicked.connect(self._resume_all)
        f1.addWidget(b)
        b = QtWidgets.QPushButton("Pause All")
        b.setObjectName("primary")
        b.clicked.connect(self._pause_all)
        f1.addWidget(b)
        trash = QtWidgets.QToolButton()
        trash.setText("🗑")
        trash.setToolTip("Remove selected (or everything in this list)")
        trash.clicked.connect(self._trash)
        f1.addWidget(trash)
        lay.addLayout(f1)

        # footer row 2: Format / Quality / Subtitle
        f2 = QtWidgets.QHBoxLayout()
        f2.setContentsMargins(14, 4, 14, 6)

        def combo(label, items, current, setter):
            f2.addWidget(QtWidgets.QLabel(label))
            c = QtWidgets.QComboBox()
            c.addItems(items)
            c.setCurrentText(current)
            c.currentTextChanged.connect(setter)
            f2.addWidget(c)
            return c

        combo("Format:", ["MP4", "MKV", "WEBM", "MP3 (audio)"], self.cfg.fmt, lambda t: self._pref("fmt", t))
        combo("Quality:", ["Best", "2160p", "1440p", "1080p", "720p", "480p", "360p"],
              self.cfg.quality, lambda t: self._pref("quality", t))
        combo("Subtitle:", ["None"] + list(E.SUBTITLE_CODES), self.cfg.subtitle,
              lambda t: self._pref("subtitle", t))
        f2.addStretch(1)
        lay.addLayout(f2)

        # footer row 3: file location
        f3 = QtWidgets.QHBoxLayout()
        f3.setContentsMargins(14, 4, 14, 12)
        f3.addWidget(QtWidgets.QLabel("File Location:"))
        self.loc = QtWidgets.QLineEdit(self.cfg.download_dir)
        self.loc.setReadOnly(True)
        f3.addWidget(self.loc, 1)
        b = QtWidgets.QPushButton("Change")
        b.setObjectName("small")
        b.clicked.connect(self._change_dir)
        f3.addWidget(b)
        fb = QtWidgets.QToolButton()
        fb.setText("📁")
        fb.setToolTip("Open folder")
        fb.clicked.connect(self._open_dir)
        f3.addWidget(fb)
        lay.addLayout(f3)
        return right

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
        n = self.proxy.rowCount()
        self.count.setText(f"{n} item" + ("" if n == 1 else "s"))
        act = self.mgr.active_count()
        spd = sum(i.speed for i in self.mgr.items if i.running)
        title = f"Grabwave {VERSION}"
        if act:
            title += f"  —  {act}/{self.cfg.max_concurrent} active  ↓ {E.fmt_size(spd) if spd else '0 B'}/s"
        if self.windowTitle() != title:
            self.setWindowTitle(title)

    # --- actions ----------------------------------------------------------
    def _clipboard_urls(self) -> list:
        import re
        text = QtWidgets.QApplication.clipboard().text() or ""
        urls = [u.rstrip(".,;)'\"") for u in re.findall(r"https?://[^\s<>\"']+", text)]
        return list(dict.fromkeys(urls))

    def _add_from_clipboard(self, ask_if_empty: bool = False):
        """Add whatever link(s) are on the clipboard and start right away (no window)."""
        urls = self._clipboard_urls()
        if not urls:
            if ask_if_empty:
                self._add_dialog(False)  # nothing copied yet: let the person type or paste a link
            else:
                self.statusBar().showMessage("No link on the clipboard", 2500)
            return
        here = self.browser.current_url().rstrip("/")
        for u in urls:
            kind = "live" if camsites.detect(u) else "download"
            it = self.mgr.add(u, kind, "")
            if kind == "download" and u.rstrip("/") == here:  # the page open in the built-in browser
                alt = self.browser.best_media()
                if alt:
                    it.alt_url, it.alt_referer = alt, u
        self.model.reset()
        self.mgr.save()
        self.tab_dl.setChecked(True)
        self._tab_changed()
        self.statusBar().showMessage(f"Added {len(urls)} link" + ("" if len(urls) == 1 else "s") + " and started", 3000)

    def keyPressEvent(self, e):
        if e.matches(QtGui.QKeySequence.Paste):  # Ctrl+V anywhere outside a text box
            self._add_from_clipboard(False)
            e.accept()
            return
        super().keyPressEvent(e)

    def _add_dialog(self, live: bool):
        d = AddDialog(self, self.cfg, live)
        if d.exec() == QtWidgets.QDialog.Accepted:
            self._add_urls(d.urls(), d.kind())

    def _add_from_browser(self, url: str):
        kind = "live" if camsites.detect(url) else "download"
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
        it = self.mgr.add(url, "live" if camsites.detect(url) else "download", "")
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

    def _card_action(self, it: E.Item):
        if it.status in ("Failed", "Paused", "Stopped"):
            self.mgr.resume(it)
        elif it.status == "Completed":
            if it.path and os.path.exists(it.path):
                open_path(it.path)
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
        self.model.reset()
        self.mgr.save()

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
        SettingsDialog(self, self.cfg).exec()

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
        self.timer.stop()
        self.mgr.shutdown()
        e.accept()


def main():
    print(f"Grabwave {VERSION} running from {Path(__file__).resolve()}")
    # Qt 6.10+ marks invalidateFilter() deprecated; it still works and the notice only adds noise
    warnings.filterwarnings("ignore", message=".*invalidateFilter.*", category=DeprecationWarning)
    E.activate_ytdlp_update()  # use a newer downloaded yt-dlp if the user updated it (must run before any import)
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
    app.setStyleSheet(STYLE)
    cfg = E.Config.load()
    mgr = E.Manager(cfg)
    w = MainWindow(mgr)
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
