"""Grabwave's look ("Paper Desk") and its extra screens: mascot, wave line, download cards, shelf,
watchlist, smart-paste toast and the finished-download splash.

Everything here is plain PySide6 painting and widgets, no extra files or fonts are needed.
"""
from __future__ import annotations

import math
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

from PySide6 import QtCore, QtGui, QtWidgets
from PySide6.QtCore import Qt

import engine as E

# ---- palette -----------------------------------------------------------------------------------
PAPER = "#f4efe6"
CARD = "#fffdf8"
LINE = "#ddd5c6"
SHADOW = "#eee6d6"
INK = "#26231f"
MUTED = "#6b6458"
FAINT = "#8a8273"
ACCENT = "#b4462b"
RED = "#c0392b"
GREEN = "#2f6b4f"
GRAY = MUTED

SITE_DOTS = {"YouTube": "#d9482b", "Vimeo": "#2a9fd1", "Facebook": "#2d6fd6", "Instagram": "#c13584",
             "TikTok": "#26231f", "Twitch": "#7a4fd1", "Dailymotion": "#2d6fd6", "Bilibili": "#00a1d6",
             "SoundCloud": "#e07b00"}
POSTER_COLORS = ["#e6dccb", "#dbc9b4", "#cfd8cf", "#d4d0e0", "#e0cfcf", "#d3dde3"]


def serif(size: float, bold: bool = True) -> QtGui.QFont:
    f = QtGui.QFont()
    f.setFamilies(["Fraunces", "Georgia", "Noto Serif", "DejaVu Serif", "serif"])
    f.setPointSizeF(size)
    f.setBold(bold)
    return f


STYLE = f"""
QWidget {{ font-family: "Inter", "Segoe UI", "Noto Sans", "Ubuntu", sans-serif; font-size: 13px; color: {INK}; }}
QMainWindow, QDialog {{ background: {PAPER}; }}
#page, #left {{ background: {PAPER}; }}
QToolTip {{ background: {INK}; color: {PAPER}; border: none; padding: 4px 8px; }}
QToolButton {{ border: none; background: transparent; padding: 4px 8px; border-radius: 8px; font-size: 17px; color: {MUTED}; }}
QToolButton:hover {{ background: #e9e1d2; color: {INK}; }}
QToolButton:disabled {{ color: #c8bfae; }}
QToolButton::menu-indicator {{ image: none; }}
QLineEdit {{ border: 1px solid {LINE}; border-radius: 8px; padding: 6px 10px; background: {CARD}; selection-background-color: {ACCENT}; selection-color: white; }}
QLineEdit:focus {{ border: 1px solid {ACCENT}; }}
QLineEdit[readOnly="true"] {{ background: #efe9dc; color: {MUTED}; }}
QPlainTextEdit {{ border: 1px solid {LINE}; border-radius: 8px; background: {CARD}; }}
QPushButton {{ border-radius: 8px; padding: 6px 14px; background: {CARD}; border: 1px solid {LINE}; }}
QPushButton:hover {{ border: 1px solid {ACCENT}; }}
QPushButton#primary {{ background: {ACCENT}; color: #fffdf8; border: none; padding: 7px 18px; font-weight: 600; }}
QPushButton#primary:hover {{ background: #9a3a22; }}
QPushButton#primary:disabled {{ background: #d9b8ad; }}
QPushButton#outline {{ background: {CARD}; border: 1px solid {LINE}; padding: 6px 14px; font-weight: 600; }}
QPushButton#small {{ padding: 3px 10px; }}
QPushButton#tab {{ border: none; background: transparent; color: {MUTED}; font-weight: 600; padding: 7px 16px; }}
QPushButton#tab:hover {{ background: #e9e1d2; color: {INK}; }}
QPushButton#tab:checked {{ background: {INK}; color: {PAPER}; }}
QPushButton#seg {{ border: none; background: transparent; color: {FAINT}; font-weight: 600; padding: 4px 4px; border-radius: 0; border-bottom: 2px solid transparent; }}
QPushButton#seg:checked {{ color: {INK}; border-bottom: 2px solid {INK}; }}
QPushButton#seg[active="true"] {{ color: {ACCENT}; }}
QPushButton#chip {{ background: {CARD}; border: 1px solid {LINE}; border-radius: 19px; padding: 10px 18px; font-weight: 600; }}
QPushButton#chip:hover {{ border: 1px solid {ACCENT}; }}
QPushButton#chipadd {{ background: transparent; border: 1.5px dashed #b9b09e; border-radius: 19px; padding: 10px 18px; color: {MUTED}; font-weight: 600; }}
QPushButton#chipadd:hover {{ border: 1.5px dashed {ACCENT}; color: {ACCENT}; }}
QFrame#pastebar {{ background: {CARD}; border: 1.5px solid {ACCENT}; border-radius: 14px; }}
QLineEdit#pasteedit {{ border: none; background: transparent; font-size: 15px; padding: 4px 6px; }}
QFrame#tray {{ background: {CARD}; border-top: 1px solid {LINE}; }}
QFrame#card, QFrame#toast {{ background: {CARD}; border: 1px solid {LINE}; border-radius: 12px; }}
QFrame#toast {{ border-radius: 14px; }}
QComboBox {{ border: 1px solid {LINE}; border-radius: 8px; padding: 4px 10px; background: {CARD}; min-width: 64px; }}
QComboBox:hover {{ border: 1px solid {ACCENT}; }}
QComboBox QAbstractItemView {{ background: {CARD}; border: 1px solid {LINE}; selection-background-color: {ACCENT}; selection-color: white; outline: 0; }}
QListView, QListWidget {{ background: transparent; border: none; outline: 0; }}
QScrollArea {{ background: transparent; border: none; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: #d2c8b5; border-radius: 4px; min-height: 30px; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QMenu {{ background: {CARD}; border: 1px solid {LINE}; border-radius: 10px; padding: 6px; }}
QMenu::item {{ padding: 7px 22px; border-radius: 6px; }}
QMenu::item:selected {{ background: #efe6d6; color: {INK}; }}
QMenu::separator {{ height: 1px; background: {LINE}; margin: 5px 8px; }}
QCheckBox::indicator {{ width: 18px; height: 18px; border-radius: 5px; border: 1.5px solid #b9b09e; background: {CARD}; }}
QCheckBox::indicator:checked {{ background: {ACCENT}; border: 1.5px solid {ACCENT}; }}
QRadioButton::indicator {{ width: 16px; height: 16px; border-radius: 9px; border: 1.5px solid #b9b09e; background: {CARD}; }}
QRadioButton::indicator:checked {{ background: {ACCENT}; border: 1.5px solid {ACCENT}; }}
QGroupBox {{ border: 1px solid {LINE}; border-radius: 12px; margin-top: 12px; padding: 12px; background: {CARD}; font-weight: 600; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 14px; padding: 0 6px; }}
QStatusBar {{ background: {PAPER}; color: {MUTED}; }}
QStatusBar::item {{ border: none; }}
"""



# ---- painted icons (same look on every computer; no emoji) ---------------------------------------
def icon_pixmap(name: str, color: str = MUTED, size: int = 20) -> QtGui.QPixmap:
    pm = QtGui.QPixmap(size * 2, size * 2)
    pm.setDevicePixelRatio(2)
    pm.fill(Qt.transparent)
    p = QtGui.QPainter(pm)
    p.setRenderHint(QtGui.QPainter.Antialiasing)
    p.scale(size / 24.0, size / 24.0)
    col = QtGui.QColor(color)
    pen = QtGui.QPen(col, 1.9, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    P = QtCore.QPointF

    def poly(*pts):
        p.drawPolyline([P(x, y) for x, y in pts])

    def line(x1, y1, x2, y2):
        p.drawLine(P(x1, y1), P(x2, y2))

    if name == "home":
        poly((4, 11), (12, 4), (20, 11))
        poly((6, 10), (6, 20), (18, 20), (18, 10))
        poly((10, 20), (10, 14), (14, 14), (14, 20))
    elif name == "back":
        line(19, 12, 5, 12)
        poly((11, 6), (5, 12), (11, 18))
    elif name == "forward":
        line(5, 12, 19, 12)
        poly((13, 6), (19, 12), (13, 18))
    elif name in ("reload", "retry"):
        p.drawArc(QtCore.QRectF(5, 5, 14, 14), 60 * 16, 280 * 16)
        poly((14.5, 1.8), (14.5, 6.2), (19, 5.4))
    elif name == "download":
        line(12, 4, 12, 15)
        poly((7, 10), (12, 15), (17, 10))
        poly((5, 16), (5, 20), (19, 20), (19, 16))
    elif name == "external":
        line(11, 13, 19, 5)
        poly((13, 5), (19, 5), (19, 11))
        poly((17, 14), (17, 19), (5, 19), (5, 7), (10, 7))
    elif name == "folder":
        poly((3, 7), (9, 7), (11, 9), (21, 9), (21, 19), (3, 19), (3, 7))
    elif name == "trash":
        line(4, 7, 20, 7)
        poly((9, 7), (9, 4), (15, 4), (15, 7))
        poly((6, 7), (7, 20), (17, 20), (18, 7))
        line(10, 11, 10, 16)
        line(14, 11, 14, 16)
    elif name == "sliders":
        for y, x in ((7, 9), (12, 15), (17, 8)):
            line(4, y, 20, y)
            p.setBrush(QtGui.QColor(PAPER))
            p.drawEllipse(P(x, y), 2.3, 2.3)
            p.setBrush(Qt.NoBrush)
    elif name == "plus":
        line(12, 5, 12, 19)
        line(5, 12, 19, 12)
    elif name == "chevron":
        poly((6, 9), (12, 15), (18, 9))
    elif name == "x":
        line(6, 6, 18, 18)
        line(18, 6, 6, 18)
    elif name == "play":
        p.setPen(Qt.NoPen)
        p.setBrush(col)
        p.drawPolygon([P(8, 5), P(19, 12), P(8, 19)])
    elif name == "pause":
        p.setPen(Qt.NoPen)
        p.setBrush(col)
        p.drawRoundedRect(QtCore.QRectF(7, 5, 3.6, 14), 1.2, 1.2)
        p.drawRoundedRect(QtCore.QRectF(13.4, 5, 3.6, 14), 1.2, 1.2)
    elif name == "help":
        p.drawEllipse(P(12, 12), 8.5, 8.5)
        q = QtGui.QPainterPath(P(9.3, 9.6))
        q.cubicTo(9.5, 6.4, 14.8, 6.4, 14.8, 9.6)
        q.cubicTo(14.8, 11.6, 12, 11.6, 12, 14)
        p.drawPath(q)
        p.setPen(Qt.NoPen)
        p.setBrush(col)
        p.drawEllipse(P(12, 17), 1.1, 1.1)
    elif name == "stop":
        p.setPen(Qt.NoPen)
        p.setBrush(col)
        p.drawRoundedRect(QtCore.QRectF(6.5, 6.5, 11, 11), 2, 2)
    p.end()
    return pm


def icon(name: str, color: str = MUTED, size: int = 20) -> QtGui.QIcon:
    return QtGui.QIcon(icon_pixmap(name, color, size))


def build_style() -> str:
    """STYLE plus the pieces that need a picture file (the drop-down chevron). Call after QApplication exists."""
    extra = ""
    try:
        d = E.config_dir() / "ui"
        d.mkdir(parents=True, exist_ok=True)
        f = d / "chevron.png"
        icon_pixmap("chevron", MUTED, 12).save(str(f), "PNG")
        url = str(f).replace("\\", "/")
        extra = f"""
QComboBox {{ padding: 5px 30px 5px 12px; border-radius: 10px; }}
QComboBox::drop-down {{ border: none; width: 26px; subcontrol-origin: padding; subcontrol-position: center right; }}
QComboBox::down-arrow {{ image: url("{url}"); width: 12px; height: 12px; }}
"""
    except Exception:
        pass
    return STYLE + extra + f"""
QToolButton#nav {{ border-radius: 18px; padding: 0; }}
QToolButton#nav:hover {{ background: #e9e1d2; }}
QToolButton#grab {{ background: {ACCENT}; border-radius: 18px; padding: 0; }}
QToolButton#grab:hover {{ background: #9a3a22; }}
QToolButton#grab:disabled {{ background: #d9b8ad; }}
QLineEdit#urlbar {{ border-radius: 18px; padding: 0 16px; min-height: 34px; background: {CARD}; }}
QProgressBar {{ border: none; background: #ece5d6; border-radius: 5px; min-height: 10px; max-height: 10px; text-align: center; color: transparent; }}
QProgressBar::chunk {{ background: {ACCENT}; border-radius: 5px; }}
QLineEdit#path {{ border-radius: 10px; padding: 5px 12px; background: #efe9dc; color: {MUTED}; }}
"""


# ---- mascot ------------------------------------------------------------------------------------
def paint_wavy(p: QtGui.QPainter, rect: QtCore.QRectF, mood: str = "working", color: str = ACCENT) -> None:
    """Draw 'Wavy' (a friendly wave blob) in rect. mood: sleepy | working | happy | oops."""
    p.save()
    p.setRenderHint(QtGui.QPainter.Antialiasing)
    s = min(rect.width(), rect.height()) / 64.0
    p.translate(rect.center().x() - 32 * s, rect.center().y() - 32 * s)
    p.scale(s, s)
    body = QtGui.QPainterPath()
    body.moveTo(8, 40)
    body.cubicTo(8, 24, 20, 12, 33, 15)
    body.cubicTo(46, 12, 58, 24, 56, 40)
    body.cubicTo(56, 52, 46, 58, 32, 58)
    body.cubicTo(18, 58, 8, 52, 8, 40)
    body.closeSubpath()
    p.setPen(Qt.NoPen)
    p.setBrush(QtGui.QColor("#9a8f7c" if mood == "oops" else color))
    p.drawPath(body)
    ink = QtGui.QPen(QtGui.QColor(INK), 2.6, Qt.SolidLine, Qt.RoundCap)
    p.setPen(ink)
    p.setBrush(Qt.NoBrush)
    if mood == "sleepy":
        p.drawLine(19, 38, 29, 38)
        p.drawLine(35, 38, 45, 38)
        p.drawLine(28, 46, 36, 46)
        f = p.font()
        f.setPixelSize(9)
        f.setBold(True)
        p.setFont(f)
        p.setPen(QtGui.QColor(MUTED))
        p.drawText(QtCore.QPointF(46, 14), "z")
        p.drawText(QtCore.QPointF(52, 8), "z")
    elif mood == "happy":
        for x0 in (19, 35):
            path = QtGui.QPainterPath(QtCore.QPointF(x0, 37))
            path.quadTo(x0 + 5, 31, x0 + 10, 37)
            p.drawPath(path)
        mouth = QtGui.QPainterPath(QtCore.QPointF(24, 44))
        mouth.quadTo(32, 54, 40, 44)
        p.drawPath(mouth)
        p.setPen(QtGui.QPen(QtGui.QColor(INK), 2, Qt.SolidLine, Qt.RoundCap))
        p.drawLine(52, 12, 52, 20)
        p.drawLine(48, 16, 56, 16)
    elif mood == "oops":
        p.setPen(Qt.NoPen)
        p.setBrush(QtGui.QColor(INK))
        p.drawEllipse(QtCore.QPointF(24, 38), 3.4, 3.4)
        p.drawEllipse(QtCore.QPointF(40, 38), 3.4, 3.4)
        p.setBrush(Qt.NoBrush)
        p.setPen(ink)
        mouth = QtGui.QPainterPath(QtCore.QPointF(26, 48))
        mouth.quadTo(32, 42, 38, 48)
        p.drawPath(mouth)
        p.setPen(QtGui.QPen(QtGui.QColor("#5aa0d6"), 3, Qt.SolidLine, Qt.RoundCap))
        tear = QtGui.QPainterPath(QtCore.QPointF(50, 20))
        tear.quadTo(52, 26, 50, 28)
        p.drawPath(tear)
    else:  # working
        p.setPen(Qt.NoPen)
        p.setBrush(QtGui.QColor(INK))
        p.drawEllipse(QtCore.QPointF(24, 37), 3.6, 3.6)
        p.drawEllipse(QtCore.QPointF(40, 37), 3.6, 3.6)
        p.setBrush(Qt.NoBrush)
        p.setPen(ink)
        mouth = QtGui.QPainterPath(QtCore.QPointF(27, 46))
        mouth.quadTo(32, 49, 37, 46)
        p.drawPath(mouth)
    p.restore()


class Wavy(QtWidgets.QWidget):
    """The mascot as a widget. The 'working' mood bobs gently while visible."""

    def __init__(self, mood: str = "sleepy", size: int = 64, parent=None):
        super().__init__(parent)
        self.mood = mood
        self.setFixedSize(size, size)
        self._t = 0.0
        self._timer = QtCore.QTimer(self)
        self._timer.timeout.connect(self._step)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)

    def set_mood(self, mood: str) -> None:
        if mood != self.mood:
            self.mood = mood
            self.update()

    def showEvent(self, e):
        self._timer.start(70)
        super().showEvent(e)

    def hideEvent(self, e):
        self._timer.stop()
        super().hideEvent(e)

    def _step(self):
        if self.mood == "working":
            self._t += 0.25
            self.update()

    def paintEvent(self, e):
        p = QtGui.QPainter(self)
        dy = math.sin(self._t) * 2.5 if self.mood == "working" else 0
        paint_wavy(p, QtCore.QRectF(0, dy, self.width(), self.height()), self.mood)


class WaveLine(QtWidgets.QWidget):
    """The thin wavy line under the header."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(10)

    def paintEvent(self, e):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        path = QtGui.QPainterPath()
        mid = self.height() / 2
        path.moveTo(0, mid)
        for x in range(0, self.width() + 2, 2):
            path.lineTo(x, mid + math.sin(x / 7.0) * 2.0)
        col = QtGui.QColor(ACCENT)
        col.setAlpha(150)
        p.setPen(QtGui.QPen(col, 1.4, Qt.SolidLine, Qt.RoundCap))
        p.drawPath(path)


def paint_wave_bar(p: QtGui.QPainter, bar: QtCore.QRectF, frac: float) -> None:
    """Progress bar whose filled end is a little wave."""
    p.save()
    p.setRenderHint(QtGui.QPainter.Antialiasing)
    clip = QtGui.QPainterPath()
    clip.addRoundedRect(bar, bar.height() / 2, bar.height() / 2)
    p.setClipPath(clip)
    p.setPen(Qt.NoPen)
    p.setBrush(QtGui.QColor("#ece5d6"))
    p.drawRect(bar)
    fw = bar.width() * max(0.0, min(1.0, frac))
    if fw > 0:
        path = QtGui.QPainterPath()
        path.moveTo(bar.left(), bar.top())
        path.lineTo(bar.left() + fw, bar.top())
        steps = 6
        for i in range(steps + 1):
            y = bar.top() + bar.height() * i / steps
            path.lineTo(bar.left() + fw + math.sin(i * 1.7) * 2.0, y)
        path.lineTo(bar.left(), bar.bottom())
        path.closeSubpath()
        p.setBrush(QtGui.QColor(ACCENT))
        p.drawPath(path)
    p.restore()


def cover_pixmap(src: QtGui.QPixmap, w: int, h: int, radius: int = 6) -> QtGui.QPixmap:
    """Scale to fill w×h (cropping the overflow) with rounded corners."""
    out = QtGui.QPixmap(w, h)
    out.fill(Qt.transparent)
    p = QtGui.QPainter(out)
    p.setRenderHints(QtGui.QPainter.Antialiasing | QtGui.QPainter.SmoothPixmapTransform)
    path = QtGui.QPainterPath()
    path.addRoundedRect(QtCore.QRectF(0, 0, w, h), radius, radius)
    p.setClipPath(path)
    sc = src.scaled(w, h, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
    p.drawPixmap((w - sc.width()) // 2, (h - sc.height()) // 2, sc)
    p.end()
    return out


# ---- thumbnails --------------------------------------------------------------------------------
class ThumbCache(QtCore.QObject):
    """Loads preview pictures in the background so the window never waits for them."""
    ready = QtCore.Signal(str)
    _done = QtCore.Signal(str, str, str)

    def __init__(self, cfg: E.Config):
        super().__init__()
        self._pix: dict = {}
        self._pending: set = set()
        self._failed: set = set()
        self._pool = ThreadPoolExecutor(max_workers=E.perf(cfg)["thumbs"])
        self._done.connect(self._on_done)

    def set_workers(self, n: int) -> None:
        old = self._pool
        self._pool = ThreadPoolExecutor(max_workers=max(1, n))
        old.shutdown(wait=False)

    def get(self, item: E.Item):
        p = self._pix.get(item.id)
        if p:
            return p
        f = E.thumb_path(item)
        if f.exists() and f.stat().st_size > 0:
            pm = QtGui.QPixmap(str(f))
            if not pm.isNull():
                self._pix[item.id] = pm
                return pm
        key = (item.id, item.path if (item.path and os.path.isfile(item.path)) else item.thumb)
        if key[1] and item.id not in self._pending and key not in self._failed:
            self._pending.add(item.id)
            self._pool.submit(self._job, item, key[1])
        return None

    def forget(self, item_id: str) -> None:
        self._pix.pop(item_id, None)
        self._pending.discard(item_id)

    def _job(self, item: E.Item, key: str) -> None:
        try:
            path = E.fetch_thumb(item)
        except Exception:
            path = ""
        self._done.emit(item.id, path, key)

    def _on_done(self, item_id: str, path: str, key: str) -> None:
        self._pending.discard(item_id)
        if not path:
            self._failed.add((item_id, key))
        if path:
            pm = QtGui.QPixmap(path)
            if not pm.isNull():
                self._pix[item_id] = pm
                self.ready.emit(item_id)


# ---- status text -------------------------------------------------------------------------------
def status_line(it: E.Item) -> tuple[str, str]:
    s = it.status
    pct = min(100, int(it.done * 100 / it.total)) if it.total else None
    if s == "Failed":
        return "Didn't work. Right-click for details.", RED
    if s == "Downloading":
        parts = [f"{pct}%" if pct is not None else E.fmt_size(it.done)]
        if it.speed:
            parts.append(E.fmt_speed(it.speed))
        if it.eta:
            parts.append(E.fmt_dur(it.eta) + " left")
        return "  ·  ".join(parts), MUTED
    if s == "Recording":
        parts = ["● Recording"]
        if it.started:
            parts.append(E.fmt_dur(time.time() - it.started))
        parts.append(E.fmt_size(it.done))
        return "  ·  ".join(parts), RED
    if s == "Retrying":
        return "Trying another way…", MUTED
    if s == "Waiting":
        return "Waiting for the stream to start…", MUTED
    if s == "Processing":
        return "Finishing up…", MUTED
    if s == "Completed":
        bits = [E.fmt_size(it.done or it.total)]
        if it.duration:
            bits.append(E.fmt_dur(it.duration))
        return "  ·  ".join(bits), MUTED
    if s == "Paused":
        return "Paused" + (f"  ·  {pct}%" if pct is not None else ""), MUTED
    if s == "Queued":
        return "Queued", MUTED
    return s, MUTED


# ---- download cards ----------------------------------------------------------------------------
class CardDelegate(QtWidgets.QStyledItemDelegate):
    """One download as a paper card: thumbnail, title, wave progress, stamp, hover buttons."""
    action = QtCore.Signal(object, str)  # item, "main" | "folder" | "remove"

    def __init__(self, view, thumbs: ThumbCache):
        super().__init__(view)
        self.thumbs = thumbs
        self.cell = QtCore.QSize(360, 92)

    def sizeHint(self, option, index):
        return self.cell

    @staticmethod
    def _geom(rect: QtCore.QRect):
        card = rect.adjusted(6, 4, -6, -6)
        thumb = QtCore.QRect(card.left() + 10, card.center().y() - 29, 88, 58)
        d = 26
        y = card.bottom() - d - 6
        btns = {"remove": QtCore.QRect(card.right() - 10 - d, y, d, d),
                "folder": QtCore.QRect(card.right() - 10 - 2 * d - 6, y, d, d),
                "main": QtCore.QRect(card.right() - 10 - 3 * d - 12, y, d, d)}
        return card, thumb, btns

    @staticmethod
    def main_glyph(it: E.Item) -> str:
        if it.status in ("Failed", "Paused", "Stopped"):
            return "retry"
        if it.status == "Completed":
            return "play"
        if it.kind == "live" and it.running:
            return "stop"
        return "pause"

    def paint(self, p, opt, idx):
        it = idx.data(Qt.UserRole)
        if it is None:
            return
        card, thumb, btns = self._geom(opt.rect)
        p.save()
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        selected = bool(opt.state & QtWidgets.QStyle.State_Selected)
        hover = bool(opt.state & QtWidgets.QStyle.State_MouseOver)
        p.setPen(Qt.NoPen)
        p.setBrush(QtGui.QColor(SHADOW))
        p.drawRoundedRect(card.translated(0, 2), 12, 12)
        p.setPen(QtGui.QPen(QtGui.QColor(ACCENT if selected else LINE), 1.5 if selected else 1))
        p.setBrush(QtGui.QColor(CARD))
        p.drawRoundedRect(card, 12, 12)

        # thumbnail
        pm = self.thumbs.get(it)
        if pm is not None:
            p.drawPixmap(thumb.topLeft(), cover_pixmap(pm, thumb.width(), thumb.height(), 8))
        else:
            p.setPen(Qt.NoPen)
            p.setBrush(QtGui.QColor(POSTER_COLORS[sum(map(ord, it.id[:6])) % len(POSTER_COLORS)]))
            p.drawRoundedRect(thumb, 8, 8)
            if it.status == "Failed":
                paint_wavy(p, QtCore.QRectF(thumb).adjusted(10, 2, -10, -2), "oops")
            else:
                p.setPen(QtGui.QColor("#fffdf8" if it.kind != "live" else RED))
                f = p.font()
                f.setBold(True)
                f.setPointSize(10 if it.kind == "live" else 16)
                p.setFont(f)
                p.drawText(thumb, Qt.AlignCenter, "LIVE" if it.kind == "live" else "▶")
        if it.kind == "live" and it.status == "Recording":
            p.setPen(Qt.NoPen)
            p.setBrush(QtGui.QColor(RED))
            p.drawEllipse(QtCore.QPointF(thumb.left() + 11, thumb.top() + 11), 4, 4)

        # text
        x = thumb.right() + 12
        stamp_w = 78 if it.status == "Completed" else 0
        w = max(40, card.right() - 10 - x - stamp_w)
        f = QtGui.QFont(p.font())
        f.setBold(True)
        f.setPointSize(10)
        p.setFont(f)
        p.setPen(QtGui.QColor(INK))
        fm = QtGui.QFontMetrics(f)
        p.drawText(QtCore.QRect(x, card.top() + 9, w, 20), Qt.AlignVCenter | Qt.AlignLeft,
                   fm.elidedText(it.title, Qt.ElideMiddle, w))
        text, color = status_line(it)
        f2 = QtGui.QFont(f)
        f2.setBold(False)
        f2.setPointSize(9)
        fm2 = QtGui.QFontMetrics(f2)
        y_text = card.top() + 33
        if it.status == "Downloading" and it.total:
            paint_wave_bar(p, QtCore.QRectF(x, card.top() + 34, w - (0 if not hover else 0), 8),
                           it.done / it.total)
            y_text = card.top() + 46
        p.setFont(f2)
        p.setPen(QtGui.QColor(color))
        tw = w if not hover else max(40, btns["main"].left() - 8 - x)
        p.drawText(QtCore.QRect(x, y_text, tw, 18), Qt.AlignVCenter | Qt.AlignLeft,
                   fm2.elidedText(text, Qt.ElideRight, tw))

        # stamp
        if it.status == "Completed":
            p.save()
            p.translate(card.right() - 46, card.top() + 21)
            p.rotate(-8)
            word = "RECORDED" if it.kind == "live" else "SAVED"
            col = QtGui.QColor(ACCENT if it.kind == "live" else GREEN)
            sf = QtGui.QFont(p.font())
            sf.setBold(True)
            sf.setPointSizeF(7.5)
            sf.setLetterSpacing(QtGui.QFont.AbsoluteSpacing, 1.2)
            p.setFont(sf)
            bw = QtGui.QFontMetrics(sf).horizontalAdvance(word) + 14
            r = QtCore.QRectF(-bw / 2, -9, bw, 18)
            p.setPen(QtGui.QPen(col, 1.8))
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(r, 5, 5)
            p.drawText(r, Qt.AlignCenter, word)
            p.restore()

        # hover buttons
        if hover:
            glyphs = {"main": self.main_glyph(it), "folder": "folder", "remove": "x"}
            for key, r in btns.items():
                p.setPen(QtGui.QPen(QtGui.QColor(LINE), 1))
                p.setBrush(QtGui.QColor(CARD))
                p.drawEllipse(r)
                pm = icon_pixmap(glyphs[key], ACCENT if key == "main" else MUTED, 14)
                p.drawPixmap(r.center().x() - 7, r.center().y() - 7, pm)
        p.restore()

    def editorEvent(self, event, model, option, index):
        if event.type() == QtCore.QEvent.MouseButtonRelease and event.button() == Qt.LeftButton:
            _, _, btns = self._geom(option.rect)
            pos = event.position().toPoint()
            for key, r in btns.items():
                if r.adjusted(-3, -3, 3, 3).contains(pos):
                    it = index.data(Qt.UserRole)
                    if it is not None:
                        self.action.emit(it, key)
                    return True
        return False


# ---- empty state -------------------------------------------------------------------------------
class EmptyState(QtWidgets.QWidget):
    def __init__(self, text: str, sub: str = "", mood: str = "sleepy", parent=None):
        super().__init__(parent)
        lay = QtWidgets.QVBoxLayout(self)
        lay.setAlignment(Qt.AlignCenter)
        lay.setSpacing(4)
        self.wavy = Wavy(mood, 56)
        lay.addWidget(self.wavy, 0, Qt.AlignHCenter)
        self.title = QtWidgets.QLabel(text)
        self.title.setFont(serif(12))
        self.title.setAlignment(Qt.AlignCenter)
        lay.addWidget(self.title)
        self.sub = QtWidgets.QLabel(sub)
        self.sub.setStyleSheet(f"color: {MUTED};")
        self.sub.setAlignment(Qt.AlignCenter)
        self.sub.setWordWrap(True)
        lay.addWidget(self.sub)


# ---- home page ---------------------------------------------------------------------------------
class Chip(QtWidgets.QPushButton):
    def __init__(self, label: str, color: str | None = None, add: bool = False):
        super().__init__(("+ " if add else "") + label)
        self.setObjectName("chipadd" if add else "chip")
        self.setCursor(Qt.PointingHandCursor)
        if color:
            pm = QtGui.QPixmap(12, 12)
            pm.fill(Qt.transparent)
            q = QtGui.QPainter(pm)
            q.setRenderHint(QtGui.QPainter.Antialiasing)
            q.setPen(Qt.NoPen)
            q.setBrush(QtGui.QColor(color))
            q.drawEllipse(1, 1, 10, 10)
            q.end()
            self.setIcon(QtGui.QIcon(pm))
            self.setIconSize(QtCore.QSize(10, 10))


class AddSiteDialog(QtWidgets.QDialog):
    """Name + web address, nothing else."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Add shortcut")
        self.setMinimumWidth(400)
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(22, 20, 22, 18)
        lay.setSpacing(6)
        title = QtWidgets.QLabel("Add shortcut")
        title.setFont(serif(14))
        lay.addWidget(title)
        lay.addSpacing(6)
        lay.addWidget(QtWidgets.QLabel("Name"))
        self.name = QtWidgets.QLineEdit()
        lay.addWidget(self.name)
        lay.addWidget(QtWidgets.QLabel("URL"))
        self.url = QtWidgets.QLineEdit()
        self.url.setPlaceholderText("https://")
        lay.addWidget(self.url)
        lay.addSpacing(10)
        row = QtWidgets.QHBoxLayout()
        row.addStretch(1)
        cancel = QtWidgets.QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        self.done = QtWidgets.QPushButton("Done")
        self.done.setObjectName("primary")
        self.done.setDefault(True)
        self.done.setEnabled(False)
        self.done.clicked.connect(self.accept)
        row.addWidget(cancel)
        row.addWidget(self.done)
        lay.addLayout(row)
        self.name.textChanged.connect(self._check)
        self.url.textChanged.connect(self._check)

    def _check(self):
        self.done.setEnabled(bool(self.name.text().strip() and self.url.text().strip()))

    def values(self):
        u = self.url.text().strip()
        if u and "://" not in u:
            u = "https://" + u
        return self.name.text().strip(), u


class HomePage(QtWidgets.QWidget):
    siteClicked = QtCore.Signal(str)
    grabRequested = QtCore.Signal(str)

    DEFAULT_SITES = [
        ("YouTube", "https://www.youtube.com"), ("Vimeo", "https://vimeo.com"),
        ("Facebook", "https://www.facebook.com"), ("Instagram", "https://www.instagram.com"),
        ("TikTok", "https://www.tiktok.com"), ("Twitch", "https://www.twitch.tv"),
        ("Dailymotion", "https://www.dailymotion.com"), ("Bilibili", "https://www.bilibili.com"),
        ("SoundCloud", "https://soundcloud.com"),
    ]

    def __init__(self, cfg: E.Config):
        super().__init__()
        self.cfg = cfg
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(24, 0, 24, 0)
        outer.addStretch(2)
        self.greeting = QtWidgets.QLabel("Where to?")
        self.greeting.setFont(serif(26))
        self.greeting.setAlignment(Qt.AlignCenter)
        outer.addWidget(self.greeting)
        outer.addSpacing(18)

        bar = QtWidgets.QFrame()
        bar.setObjectName("pastebar")
        bar.setFixedWidth(640)
        bar.setFixedHeight(54)
        bl = QtWidgets.QHBoxLayout(bar)
        bl.setContentsMargins(14, 6, 8, 6)
        self.paste = QtWidgets.QLineEdit()
        self.paste.setObjectName("pasteedit")
        self.paste.setPlaceholderText("Paste a video link and press Enter, or search the web")
        self.paste.returnPressed.connect(self._go)
        bl.addWidget(self.paste, 1)
        go = QtWidgets.QPushButton("Grab it")
        go.setObjectName("primary")
        go.setCursor(Qt.PointingHandCursor)
        go.clicked.connect(self._go)
        bl.addWidget(go)
        outer.addWidget(bar, 0, Qt.AlignHCenter)
        self.hint = QtWidgets.QLabel("")
        self.hint.setStyleSheet(f"color: {ACCENT}; font-weight: 600;")
        self.hint.setAlignment(Qt.AlignCenter)
        self.hint.setFixedHeight(26)
        outer.addWidget(self.hint)
        outer.addSpacing(10)

        self.chips = QtWidgets.QVBoxLayout()
        self.chips.setSpacing(12)
        outer.addLayout(self.chips)
        outer.addStretch(3)
        self._clip_url = ""
        self.rebuild()

    # greeting by time of day
    def showEvent(self, e):
        h = time.localtime().tm_hour
        part = "morning" if h < 12 else "afternoon" if h < 18 else "evening"
        self.greeting.setText(f"Good {part}. Where to?")
        super().showEvent(e)

    def set_clipboard_url(self, url: str) -> None:
        """Called when a link is on the clipboard: put it in the bar and say so."""
        self._clip_url = url
        if url and not self.paste.text().strip():
            self.paste.setText(url)
            self.hint.setText("Link detected on your clipboard. Press Enter to grab it.")
        elif not url:
            self.hint.setText("")

    def _go(self):
        t = self.paste.text().strip()
        if not t:
            return
        if t.lower().startswith(("http://", "https://")):
            self.paste.clear()
            self.hint.setText("")
            self.grabRequested.emit(t)
        else:
            self.siteClicked.emit(t)

    def rebuild(self):
        while self.chips.count():
            it = self.chips.takeAt(0)
            lay = it.layout()
            if lay is not None:
                while lay.count():
                    w = lay.takeAt(0).widget()
                    if w:
                        w.deleteLater()
        palette = ["#5b3df5", "#0e9f8e", "#d9534f", "#2d7ff9", "#e08a00", "#7a4fd1", "#2f9e44", "#c2255c"]
        entries = [(n, u, SITE_DOTS.get(n, "#7a7a7a")) for n, u in self.DEFAULT_SITES]
        for s in self.cfg.custom_sites:
            entries.append((s[0], s[1], palette[sum(map(ord, s[0])) % len(palette)]))
        custom_from = len(self.DEFAULT_SITES)
        widgets = []
        for i, (name, url, color) in enumerate(entries):
            c = Chip(name, color)
            c.clicked.connect(lambda _=False, u=url: self.siteClicked.emit(u))
            if i >= custom_from:
                c.setContextMenuPolicy(Qt.ActionsContextMenu)
                rm = QtGui.QAction("Remove shortcut", c)
                rm.triggered.connect(lambda _=False, n=name, u=url: self._remove_site(n, u))
                c.addAction(rm)
            widgets.append(c)
        add = Chip("Add site", None, add=True)
        add.setToolTip("Add a shortcut: a name and a web address")
        add.clicked.connect(self._add_site)
        widgets.append(add)
        for r in range(0, len(widgets), 5):
            row = QtWidgets.QHBoxLayout()
            row.setSpacing(12)
            row.addStretch(1)
            for w in widgets[r:r + 5]:
                row.addWidget(w)
            row.addStretch(1)
            self.chips.addLayout(row)

    def _add_site(self):
        d = AddSiteDialog(self)
        if d.exec() != QtWidgets.QDialog.Accepted:
            return
        name, url = d.values()
        if not name or not url:
            return
        self.cfg.custom_sites.append([name, url])
        self.cfg.save()
        self.rebuild()

    def _remove_site(self, name: str, url: str):
        self.cfg.custom_sites = [s for s in self.cfg.custom_sites if list(s)[:2] != [name, url]]
        self.cfg.save()
        self.rebuild()


# ---- shelf -------------------------------------------------------------------------------------
def site_of(url: str) -> str:
    h = urlparse(url).netloc.lower()
    return h[4:] if h.startswith("www.") else h


class ShelfPage(QtWidgets.QWidget):
    openRequested = QtCore.Signal(object)
    folderRequested = QtCore.Signal(object)
    convertRequested = QtCore.Signal(object, str)
    trimRequested = QtCore.Signal(object)
    removeRequested = QtCore.Signal(object, bool)

    POSTER = QtCore.QSize(148, 222)

    def __init__(self, mgr: E.Manager, thumbs: ThumbCache):
        super().__init__()
        self.mgr = mgr
        self.thumbs = thumbs
        self._sig = None
        self._dirty = True
        thumbs.ready.connect(lambda _id: setattr(self, "_dirty", True))
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(22, 14, 22, 8)
        head = QtWidgets.QHBoxLayout()
        title = QtWidgets.QLabel("Your shelf")
        title.setFont(serif(20))
        head.addWidget(title)
        self.count = QtWidgets.QLabel("")
        self.count.setStyleSheet(f"color: {MUTED};")
        head.addWidget(self.count)
        head.addStretch(1)
        self.search = QtWidgets.QLineEdit()
        self.search.setPlaceholderText("Search your shelf")
        self.search.setFixedWidth(240)
        self.search.textChanged.connect(self._mark)
        head.addWidget(self.search)
        self.sort = QtWidgets.QComboBox()
        self.sort.addItems(["Newest first", "Oldest first", "Name A–Z", "Largest first"])
        self.sort.currentIndexChanged.connect(self._mark)
        head.addWidget(self.sort)
        self.site = QtWidgets.QComboBox()
        self.site.addItem("All sites")
        self.site.currentIndexChanged.connect(self._mark)
        head.addWidget(self.site)
        lay.addLayout(head)

        self.list = QtWidgets.QListWidget()
        self.list.setViewMode(QtWidgets.QListView.IconMode)
        self.list.setResizeMode(QtWidgets.QListView.Adjust)
        self.list.setMovement(QtWidgets.QListView.Static)
        self.list.setIconSize(self.POSTER)
        self.list.setGridSize(QtCore.QSize(self.POSTER.width() + 30, self.POSTER.height() + 64))
        self.list.setWordWrap(True)
        self.list.setSpacing(6)
        self.list.setUniformItemSizes(True)
        self.list.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        self.list.setVerticalScrollMode(QtWidgets.QAbstractItemView.ScrollPerPixel)
        self.list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._menu)
        self.list.itemDoubleClicked.connect(lambda i: self.openRequested.emit(i.data(Qt.UserRole)))
        self.empty = EmptyState("Your shelf is empty", "Finished downloads and recordings appear here.")
        self.stack = QtWidgets.QStackedWidget()
        self.stack.addWidget(self.empty)
        self.stack.addWidget(self.list)
        lay.addWidget(self.stack, 1)

    def _mark(self, *_):
        self._dirty = True
        self.refresh(force=True)

    def _done_items(self) -> list:
        return [i for i in self.mgr.items if i.status == "Completed" and i.path and os.path.isfile(i.path)]

    def refresh(self, force: bool = False) -> None:
        done = self._done_items()
        sig = (tuple((i.id, i.path) for i in done), self.search.text(), self.sort.currentIndex(),
               self.site.currentIndex())
        if not (force or self._dirty or sig != self._sig):
            return
        self._sig, self._dirty = sig, False
        sites = sorted({site_of(i.url) for i in done})
        cur = self.site.currentText()
        if [self.site.itemText(k) for k in range(1, self.site.count())] != sites:
            self.site.blockSignals(True)
            self.site.clear()
            self.site.addItem("All sites")
            self.site.addItems(sites)
            self.site.setCurrentText(cur if cur in sites else "All sites")
            self.site.blockSignals(False)
        q = self.search.text().strip().lower()
        sel_site = self.site.currentText()
        shown = [i for i in done if (not q or q in i.title.lower())
                 and (sel_site == "All sites" or site_of(i.url) == sel_site)]
        k = self.sort.currentIndex()
        if k == 0:
            shown.sort(key=lambda i: i.added, reverse=True)
        elif k == 1:
            shown.sort(key=lambda i: i.added)
        elif k == 2:
            shown.sort(key=lambda i: i.title.lower())
        else:
            shown.sort(key=lambda i: os.path.getsize(i.path), reverse=True)
        total = sum(os.path.getsize(i.path) for i in done)
        self.count.setText(f"{len(done)} item{'s' if len(done) != 1 else ''} · {E.fmt_size(total)}" if done else "")
        self.list.clear()
        for it in shown:
            li = QtWidgets.QListWidgetItem(QtGui.QIcon(self._poster(it)),
                                           f"{it.title}\n{self._sub(it)}")
            li.setData(Qt.UserRole, it)
            li.setToolTip(it.path)
            li.setSizeHint(self.list.gridSize())
            self.list.addItem(li)
        self.stack.setCurrentIndex(1 if done else 0)
        if done and not shown:
            self.empty.title.setText("Nothing matches")
            self.empty.sub.setText("Try a different search or site.")
            self.stack.setCurrentIndex(0)
        elif not done:
            self.empty.title.setText("Your shelf is empty")
            self.empty.sub.setText("Finished downloads and recordings appear here.")

    @staticmethod
    def _sub(it: E.Item) -> str:
        when = time.strftime("%b %d", time.localtime(it.added))
        kind = "Recorded" if it.kind == "live" else "Saved"
        return f"{kind} {when} · {E.fmt_size(os.path.getsize(it.path))}"

    def _poster(self, it: E.Item) -> QtGui.QPixmap:
        w, h = self.POSTER.width(), self.POSTER.height()
        pm = self.thumbs.get(it)
        if pm is not None:
            out = cover_pixmap(pm, w, h, 8)
        else:
            out = QtGui.QPixmap(w, h)
            out.fill(Qt.transparent)
            q = QtGui.QPainter(out)
            q.setRenderHint(QtGui.QPainter.Antialiasing)
            q.setPen(Qt.NoPen)
            q.setBrush(QtGui.QColor(POSTER_COLORS[sum(map(ord, it.id[:6])) % len(POSTER_COLORS)]))
            q.drawRoundedRect(0, 0, w, h, 8, 8)
            paint_wavy(q, QtCore.QRectF(w / 2 - 28, h / 2 - 28, 56, 56), "sleepy", "#fffdf8")
            q.end()
        q = QtGui.QPainter(out)
        q.setRenderHint(QtGui.QPainter.Antialiasing)
        if it.duration or Path_suffix(it.path) in (".mp3", ".m4a", ".opus"):
            label = E.fmt_dur(it.duration) if it.duration else Path_suffix(it.path)[1:].upper()
            if it.kind == "live":
                label = "REC " + label if it.duration else "REC"
            f = QtGui.QFont(q.font())
            f.setBold(True)
            f.setPointSize(8)
            q.setFont(f)
            bw = QtGui.QFontMetrics(f).horizontalAdvance(label) + 14
            r = QtCore.QRectF(8, h - 28, bw, 20)
            q.setPen(Qt.NoPen)
            q.setBrush(QtGui.QColor(ACCENT if it.kind == "live" else INK))
            q.drawRoundedRect(r, 5, 5)
            q.setPen(QtGui.QColor(PAPER))
            q.drawText(r, Qt.AlignCenter, label)
        q.end()
        return out

    def _selected(self) -> list:
        return [i.data(Qt.UserRole) for i in self.list.selectedItems()]

    def _menu(self, pos):
        sel = self._selected()
        if not sel:
            return
        it = sel[0]
        m = QtWidgets.QMenu(self)
        m.addAction("Play", lambda: self.openRequested.emit(it))
        m.addAction("Show in folder", lambda: self.folderRequested.emit(it))
        m.addSeparator()
        t = m.addAction("Tools")
        t.setEnabled(False)
        m.addAction("Convert to MP3", lambda: self.convertRequested.emit(it, "mp3"))
        m.addAction("Convert to MP4", lambda: self.convertRequested.emit(it, "mp4"))
        m.addAction("Trim…", lambda: self.trimRequested.emit(it))
        m.addSeparator()
        m.addAction("Remove from shelf", lambda: [self.removeRequested.emit(i, False) for i in sel])
        m.addAction("Delete file", lambda: [self.removeRequested.emit(i, True) for i in sel])
        m.exec(self.list.viewport().mapToGlobal(pos))


def Path_suffix(p: str) -> str:
    return os.path.splitext(p or "")[1].lower()


class TrimDialog(QtWidgets.QDialog):
    def __init__(self, parent, title: str):
        super().__init__(parent)
        self.setWindowTitle("Trim")
        self.setMinimumWidth(380)
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(22, 20, 22, 18)
        h = QtWidgets.QLabel("Trim")
        h.setFont(serif(14))
        lay.addWidget(h)
        lay.addWidget(QtWidgets.QLabel(title))
        form = QtWidgets.QFormLayout()
        self.start = QtWidgets.QLineEdit()
        self.start.setPlaceholderText("0:00 (the beginning)")
        self.end = QtWidgets.QLineEdit()
        self.end.setPlaceholderText("1:30 (leave empty for the end)")
        form.addRow("Start at:", self.start)
        form.addRow("End at:", self.end)
        lay.addLayout(form)
        note = QtWidgets.QLabel("Times look like 90, 1:30 or 0:01:30. The original file is kept; the trimmed "
                                "copy is saved next to it.")
        note.setWordWrap(True)
        note.setStyleSheet(f"color: {MUTED};")
        lay.addWidget(note)
        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def values(self):
        return self.start.text().strip(), self.end.text().strip()


# ---- watchlist ---------------------------------------------------------------------------------
LIMITS = [("No limit", 0, 0), ("Stop after 1 hour", 60, 0), ("Stop after 2 hours", 120, 0),
          ("Stop after 4 hours", 240, 0), ("Stop at 2 GB", 0, 2048), ("Stop at 5 GB", 0, 5120)]
ACTIVE = ("Queued", "Downloading", "Recording", "Waiting", "Processing")


class Watcher(QtCore.QObject):
    """Quietly checks the watchlist and starts a recording when a room goes live."""
    wentLive = QtCore.Signal(str)
    changed = QtCore.Signal()
    _result = QtCore.Signal(str, bool)

    def __init__(self, mgr: E.Manager, cfg: E.Config):
        super().__init__()
        self.mgr, self.cfg = mgr, cfg
        self._next: dict = {}
        self._busy: set = set()
        self._first_seen = time.time() + 3
        self._result.connect(self._on_result)
        self._timer = QtCore.QTimer(self)
        self._timer.timeout.connect(self._poll)
        self._timer.start(4000)

    def enabled_count(self) -> int:
        return sum(1 for e in self.cfg.watchlist if e.get("enabled", True))

    def active_item(self, url: str):
        return next((i for i in self.mgr.items if i.url == url and i.status in ACTIVE), None)

    def check_now(self, url: str) -> None:
        self._next[url] = 0

    def _poll(self):
        now = time.time()
        if now < self._first_seen:
            return
        for e in self.cfg.watchlist:
            url = e.get("url", "")
            if not url or not e.get("enabled", True) or url in self._busy or self.active_item(url):
                continue
            if now >= self._next.get(url, 0):
                self._busy.add(url)
                self._next[url] = now + max(30, int(self.cfg.watch_every))
                threading.Thread(target=self._check, args=(url,), daemon=True).start()

    def _check(self, url: str) -> None:
        self._result.emit(url, E.check_live(url, self.cfg))

    def _on_result(self, url: str, live: bool) -> None:
        self._busy.discard(url)
        entry = next((e for e in self.cfg.watchlist if e.get("url") == url), None)
        if live and entry and entry.get("enabled", True) and not self.active_item(url):
            it = self.mgr.add(url, "live", self.cfg.live_dir)
            it.limit_min = int(entry.get("limit_min", 0))
            it.limit_mb = int(entry.get("limit_mb", 0))
            it.title = entry.get("name") or url
            self.mgr.save()
            self.wentLive.emit(entry.get("name") or url)
        self.changed.emit()

    def status_text(self, e: dict) -> tuple[str, str]:
        url = e.get("url", "")
        if not e.get("enabled", True):
            return "Paused. Not being checked.", MUTED
        it = self.active_item(url)
        if it is not None and it.status in ("Recording", "Processing"):
            return f"● Live now · recording {E.fmt_dur(time.time() - it.started) if it.started else ''}", RED
        if it is not None:
            return "Starting a recording…", MUTED
        if url in self._busy:
            return "Checking…", MUTED
        wait = int(self._next.get(url, 0) - time.time())
        if wait > 0:
            return f"Offline · next check in {E.fmt_dur(wait)}", MUTED
        return "Waiting for the first check…", MUTED


class WatchPage(QtWidgets.QWidget):
    changed = QtCore.Signal()

    def __init__(self, cfg: E.Config, watcher: Watcher):
        super().__init__()
        self.cfg, self.watcher = cfg, watcher
        self._labels: dict = {}
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(22, 14, 22, 8)
        head = QtWidgets.QHBoxLayout()
        title = QtWidgets.QLabel("Watchlist")
        title.setFont(serif(20))
        head.addWidget(title)
        sub = QtWidgets.QLabel("Grabwave checks these quietly and starts recording when a room goes live.")
        sub.setStyleSheet(f"color: {MUTED};")
        head.addWidget(sub, 1)
        lay.addLayout(head)
        add = QtWidgets.QHBoxLayout()
        self.name = QtWidgets.QLineEdit()
        self.name.setPlaceholderText("Name (optional)")
        self.name.setFixedWidth(180)
        self.url = QtWidgets.QLineEdit()
        self.url.setPlaceholderText("Paste a room or channel link to watch")
        self.url.returnPressed.connect(self._add)
        b = QtWidgets.QPushButton("+ Watch")
        b.setObjectName("primary")
        b.clicked.connect(self._add)
        add.addWidget(self.name)
        add.addWidget(self.url, 1)
        add.addWidget(b)
        lay.addLayout(add)
        self.empty = EmptyState("Nothing watched yet", "Add a link above. Public rooms and channels only.")
        self.holder = QtWidgets.QWidget()
        self.rows = QtWidgets.QVBoxLayout(self.holder)
        self.rows.setContentsMargins(0, 6, 0, 6)
        self.rows.setSpacing(10)
        self.rows.addStretch(1)
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.holder)
        self.stack = QtWidgets.QStackedWidget()
        self.stack.addWidget(self.empty)
        self.stack.addWidget(scroll)
        lay.addWidget(self.stack, 1)
        foot = QtWidgets.QLabel("Grabwave keeps checking while it is open, and in the system tray after you close the "
                                "window (turn that off in Settings).")
        foot.setWordWrap(True)
        foot.setStyleSheet(f"color: {FAINT};")
        lay.addWidget(foot)
        watcher.changed.connect(self.refresh_status)
        self.rebuild()

    def _add(self):
        u = self.url.text().strip()
        if not u:
            return
        if "://" not in u:
            u = "https://" + u
        nm = self.name.text().strip() or E.cam_username(u) or urlparse(u).netloc
        self.cfg.watchlist.append({"url": u, "name": nm, "enabled": True, "limit_min": 0, "limit_mb": 0})
        self.cfg.save()
        self.url.clear()
        self.name.clear()
        self.watcher.check_now(u)
        self.rebuild()
        self.changed.emit()

    def rebuild(self):
        while self.rows.count() > 1:
            w = self.rows.takeAt(0).widget()
            if w:
                w.deleteLater()
        self._labels = {}
        for idx, e in enumerate(self.cfg.watchlist):
            card = QtWidgets.QFrame()
            card.setObjectName("card")
            h = QtWidgets.QHBoxLayout(card)
            h.setContentsMargins(16, 12, 16, 12)
            h.setSpacing(14)
            av = QtWidgets.QLabel((e.get("name") or "?")[:1].upper())
            av.setFixedSize(40, 40)
            av.setAlignment(Qt.AlignCenter)
            av.setStyleSheet(f"background:#e6dccb;border-radius:20px;font-weight:700;color:{INK};border:none;")
            h.addWidget(av)
            col = QtWidgets.QVBoxLayout()
            col.setSpacing(2)
            nm = QtWidgets.QLabel(e.get("name") or e.get("url"))
            nm.setStyleSheet("font-weight:600;border:none;")
            nm.setToolTip(e.get("url", ""))
            st = QtWidgets.QLabel("")
            st.setStyleSheet(f"color:{MUTED};border:none;")
            col.addWidget(nm)
            col.addWidget(st)
            h.addLayout(col, 1)
            self._labels[e["url"]] = st
            lim = QtWidgets.QComboBox()
            for label, _, _ in LIMITS:
                lim.addItem(label)
            cur = next((k for k, (_, m, g) in enumerate(LIMITS)
                        if m == e.get("limit_min", 0) and g == e.get("limit_mb", 0)), 0)
            lim.setCurrentIndex(cur)
            lim.currentIndexChanged.connect(lambda k, ent=e: self._set_limit(ent, k))
            h.addWidget(lim)
            on = QtWidgets.QCheckBox("On")
            on.setChecked(e.get("enabled", True))
            on.toggled.connect(lambda v, ent=e: self._toggle(ent, v))
            h.addWidget(on)
            rm = QtWidgets.QToolButton()
            rm.setIcon(icon("x", MUTED, 14))
            rm.setToolTip("Stop watching")
            rm.clicked.connect(lambda _=False, i=idx: self._remove(i))
            h.addWidget(rm)
            self.rows.insertWidget(self.rows.count() - 1, card)
        self.stack.setCurrentIndex(1 if self.cfg.watchlist else 0)
        self.refresh_status()

    def refresh_status(self):
        for e in self.cfg.watchlist:
            lab = self._labels.get(e.get("url"))
            if lab is not None:
                text, color = self.watcher.status_text(e)
                lab.setText(text)
                lab.setStyleSheet(f"color:{color};border:none;" + ("font-weight:600;" if color == RED else ""))

    def _set_limit(self, e: dict, k: int):
        e["limit_min"], e["limit_mb"] = LIMITS[k][1], LIMITS[k][2]
        self.cfg.save()

    def _toggle(self, e: dict, v: bool):
        e["enabled"] = v
        self.cfg.save()
        if v:
            self.watcher.check_now(e["url"])
        self.refresh_status()
        self.changed.emit()

    def _remove(self, idx: int):
        if 0 <= idx < len(self.cfg.watchlist):
            self.cfg.watchlist.pop(idx)
            self.cfg.save()
            self.rebuild()
            self.changed.emit()


# ---- smart paste toast -------------------------------------------------------------------------
class SmartToast(QtWidgets.QFrame):
    """'Grab this link?' card that slides in when a link is copied."""
    grab = QtCore.Signal(str, str)      # url, quality ("" = default)
    _probed = QtCore.Signal(str, object, object)

    def __init__(self, parent, cfg: E.Config):
        super().__init__(parent)
        self.setObjectName("toast")
        self.cfg = cfg
        self.url = ""
        self.setFixedWidth(440)
        self.hide()
        self._probed.connect(self._on_probe)
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(10)
        top = QtWidgets.QHBoxLayout()
        self.wavy = Wavy("working", 40)
        top.addWidget(self.wavy)
        col = QtWidgets.QVBoxLayout()
        col.setSpacing(1)
        self.head = QtWidgets.QLabel("Grab this link?")
        self.head.setStyleSheet("font-weight:700;border:none;")
        self.line = QtWidgets.QLabel("")
        self.line.setStyleSheet(f"color:{MUTED};border:none;")
        col.addWidget(self.head)
        col.addWidget(self.line)
        top.addLayout(col, 1)
        lay.addLayout(top)
        mid = QtWidgets.QHBoxLayout()
        self.pic = QtWidgets.QLabel()
        self.pic.setFixedSize(84, 56)
        self.pic.setStyleSheet("background:#e6dccb;border-radius:8px;border:none;")
        mid.addWidget(self.pic)
        info = QtWidgets.QVBoxLayout()
        info.setSpacing(2)
        self.title = QtWidgets.QLabel("Looking up the video…")
        self.title.setStyleSheet("font-weight:600;border:none;")
        self.meta = QtWidgets.QLabel("")
        self.meta.setStyleSheet(f"color:{MUTED};border:none;")
        info.addWidget(self.title)
        info.addWidget(self.meta)
        mid.addLayout(info, 1)
        self.quality = QtWidgets.QComboBox()
        self.quality.addItems(["Best", "1080p", "720p", "480p", "360p"])
        mid.addWidget(self.quality)
        lay.addLayout(mid)
        row = QtWidgets.QHBoxLayout()
        row.addStretch(1)
        no = QtWidgets.QPushButton("Not now")
        no.setStyleSheet("border:none;background:transparent;color:%s;font-weight:600;" % MUTED)
        no.clicked.connect(self.hide)
        yes = QtWidgets.QPushButton("Grab it")
        yes.setObjectName("primary")
        yes.clicked.connect(self._yes)
        row.addWidget(no)
        row.addWidget(yes)
        lay.addLayout(row)
        self._timer = QtCore.QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.hide)

    def offer(self, url: str) -> None:
        self.url = url
        fm = QtGui.QFontMetrics(self.line.font())
        self.line.setText(fm.elidedText(url, Qt.ElideMiddle, 340))
        self.title.setText("Looking up the video…")
        self.meta.setText("")
        self.pic.clear()
        self.pic.setPixmap(QtGui.QPixmap())
        cur = self.cfg.quality if self.cfg.quality in [self.quality.itemText(i) for i in range(5)] else "Best"
        self.quality.setCurrentText(cur)
        self.adjustSize()
        self.show()
        self.raise_()
        self.reposition()
        self._timer.start(25000)
        threading.Thread(target=self._probe, args=(url,), daemon=True).start()

    def reposition(self, bottom: int | None = None) -> None:
        p = self.parentWidget()
        if p is None:
            return
        if bottom is not None:
            self._bottom = bottom
        b = getattr(self, "_bottom", p.height())
        self.move(22, max(10, b - self.height() - 14))

    def _probe(self, url: str) -> None:
        info, data = {}, None
        try:
            info = E.probe(url, self.cfg) or {}
            th = info.get("thumbnail") or ""
            if th.lower().startswith(("http://", "https://")):
                import urllib.request
                req = urllib.request.Request(th, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=10) as r:
                    data = r.read(3_000_000)
        except Exception as e:  # noqa: BLE001
            info = {"error": str(e)}
        self._probed.emit(url, info, data)

    def _on_probe(self, url: str, info, data) -> None:
        if url != self.url or not self.isVisible():
            return
        if info.get("live"):
            self.head.setText("Record this live room?")
            self.title.setText(info.get("title", ""))
            self.meta.setText("Grabwave will record it as soon as it is live.")
            self.quality.hide()
        else:
            self.quality.show()
            if info.get("error") or not info:
                self.title.setText("Couldn't preview this link")
                self.meta.setText("You can still try to grab it.")
            else:
                fm = QtGui.QFontMetrics(self.title.font())
                self.title.setText(fm.elidedText(info.get("title", ""), Qt.ElideRight, 230))
                bits = []
                if info.get("duration"):
                    bits.append(E.fmt_dur(info["duration"]))
                if info.get("size"):
                    bits.append("about " + E.fmt_size(info["size"]))
                self.meta.setText(" · ".join(bits))
                hs = info.get("heights") or []
                if hs:
                    opts = ["Best"] + [f"{h}p" for h in (1080, 720, 480, 360) if any(x >= h for x in hs)]
                    cur = self.quality.currentText()
                    self.quality.clear()
                    self.quality.addItems(opts)
                    self.quality.setCurrentText(cur if cur in opts else "Best")
        if data:
            pm = QtGui.QPixmap()
            if pm.loadFromData(data):
                self.pic.setPixmap(cover_pixmap(pm, 84, 56, 8))

    def _yes(self):
        q = "" if self.quality.isHidden() or self.quality.currentText() == self.cfg.quality else self.quality.currentText()
        url = self.url
        self.hide()
        self.grab.emit(url, q)


# ---- finished splash ---------------------------------------------------------------------------
class Splash(QtWidgets.QWidget):
    """A one-second wave splash with Wavy cheering, shown when something finishes."""

    def __init__(self, parent):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.resize(440, 150)
        self.text = ""
        self.sub = ""
        self.t0 = 0.0
        self.hide()
        self._timer = QtCore.QTimer(self)
        self._timer.timeout.connect(self._tick)

    def play(self, text: str, sub: str = "", bottom: int | None = None) -> None:
        p = self.parentWidget()
        self.text, self.sub, self.t0 = text, sub, time.time()
        b = bottom if bottom is not None else p.height()
        self.move(p.width() - self.width() - 26, max(10, b - self.height() - 16))
        self.show()
        self.raise_()
        self._timer.start(33)

    def _tick(self):
        if time.time() - self.t0 > 2.8:
            self._timer.stop()
            self.hide()
            return
        self.update()

    def paintEvent(self, e):
        t = time.time() - self.t0
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        p.setOpacity(max(0.0, min(1.0, (2.8 - t) / 0.6, t / 0.15 + 0.2)))
        r = QtCore.QRectF(self.rect()).adjusted(2, 2, -2, -2)
        clip = QtGui.QPainterPath()
        clip.addRoundedRect(r, 16, 16)
        p.setClipPath(clip)
        p.fillRect(r, QtGui.QColor(INK))
        base = r.bottom() - 56 - min(1.0, t / 0.5) * 8
        for layer, (alpha, off) in enumerate(((120, 0.0), (255, 1.3))):
            path = QtGui.QPainterPath()
            path.moveTo(r.left(), r.bottom())
            for x in range(int(r.left()), int(r.right()) + 6, 6):
                path.lineTo(x, base + layer * 14 + math.sin(x / 38.0 + t * 5 + off) * 6)
            path.lineTo(r.right(), r.bottom())
            col = QtGui.QColor(ACCENT)
            col.setAlpha(alpha)
            p.setPen(Qt.NoPen)
            p.setBrush(col)
            p.drawPath(path)
        bob = abs(math.sin(t * 6)) * 8 * max(0.0, 1 - t / 2.2)
        paint_wavy(p, QtCore.QRectF(r.center().x() - 30, r.top() + 8 - bob, 60, 60), "happy", PAPER)
        p.setPen(QtGui.QColor(PAPER))
        p.setFont(serif(11))
        fm = QtGui.QFontMetrics(p.font())
        p.drawText(QtCore.QRectF(r.left() + 12, r.bottom() - 52, r.width() - 24, 26), Qt.AlignCenter,
                   fm.elidedText(self.text, Qt.ElideRight, int(r.width() - 30)))
        if self.sub:
            f = QtGui.QFont(p.font())
            f.setBold(False)
            f.setPointSize(9)
            p.setFont(f)
            p.drawText(QtCore.QRectF(r.left() + 12, r.bottom() - 28, r.width() - 24, 20), Qt.AlignCenter, self.sub)


# ---- update dialog -----------------------------------------------------------------------------
class UpdateDialog(QtWidgets.QDialog):
    """'Grabwave X is ready': Update now / Remind me later. Downloads and installs in the background."""
    quitRequested = QtCore.Signal()
    _progress = QtCore.Signal(int, int)
    _result = QtCore.Signal(str)

    def __init__(self, parent, entry: dict, current: str, proxy: str = "", busy: int = 0):
        super().__init__(parent)
        import updater as U
        self._U = U
        self.entry, self.proxy = entry, proxy
        self._cancel = threading.Event()
        self._path = None
        self._working = False
        self.setWindowTitle("Update available")
        self.setMinimumWidth(460)
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(24, 22, 24, 18)
        lay.setSpacing(10)
        top = QtWidgets.QHBoxLayout()
        top.addWidget(Wavy("happy", 56))
        col = QtWidgets.QVBoxLayout()
        col.setSpacing(2)
        t = QtWidgets.QLabel(f"Grabwave {entry['version']} is ready")
        t.setFont(serif(15))
        col.addWidget(t)
        s = QtWidgets.QLabel(f"You are using version {current}.")
        s.setStyleSheet(f"color: {MUTED};")
        col.addWidget(s)
        top.addLayout(col, 1)
        lay.addLayout(top)
        if entry.get("notes"):
            lab = QtWidgets.QLabel("What's new")
            lab.setStyleSheet("font-weight: 600;")
            lay.addWidget(lab)
            notes = QtWidgets.QPlainTextEdit(entry["notes"])
            notes.setReadOnly(True)
            notes.setMaximumHeight(130)
            lay.addWidget(notes)
        if busy:
            w = QtWidgets.QLabel(f"{busy} download(s) are running. They will be paused during the update.")
            w.setWordWrap(True)
            w.setStyleSheet(f"color: {MUTED};")
            lay.addWidget(w)
        self.bar = QtWidgets.QProgressBar()
        self.bar.setRange(0, 100)
        self.bar.hide()
        lay.addWidget(self.bar)
        self.status = QtWidgets.QLabel("")
        self.status.setWordWrap(True)
        self.status.setStyleSheet(f"color: {MUTED};")
        lay.addWidget(self.status)
        row = QtWidgets.QHBoxLayout()
        row.addStretch(1)
        self.later = QtWidgets.QPushButton("Remind me later")
        self.later.clicked.connect(self._later)
        self.now = QtWidgets.QPushButton("Update now")
        self.now.setObjectName("primary")
        self.now.setDefault(True)
        self.now.clicked.connect(self._start)
        row.addWidget(self.later)
        row.addWidget(self.now)
        lay.addLayout(row)
        self._progress.connect(self._on_progress)
        self._result.connect(self._on_result)

    def _later(self):
        if self._working:
            self._cancel.set()
        self.reject()

    def reject(self):
        if self._working:
            self._cancel.set()
        super().reject()

    def _start(self):
        if not self._U.can_self_install():
            self.status.setText("This copy runs from source code, so it can't update itself. "
                                "Download the new installer instead.")
            return
        self._working = True
        self.now.setEnabled(False)
        self.later.setText("Cancel")
        self.bar.show()
        self.status.setText("Downloading the update…")

        def work():
            try:
                self._path = self._U.download(self.entry, lambda d, t: self._progress.emit(d, t),
                                              self._cancel, self.proxy)
                self._result.emit("")
            except self._U.Cancelled:
                self._result.emit("cancelled")
            except Exception as e:  # noqa: BLE001
                self._result.emit(str(e))

        threading.Thread(target=work, daemon=True).start()

    def _on_progress(self, done: int, total: int):
        if total:
            self.bar.setValue(int(done * 100 / total))
            self.status.setText(f"Downloading the update… {E.fmt_size(done)} of {E.fmt_size(total)}")
        else:
            self.bar.setRange(0, 0)

    def _on_result(self, msg: str):
        if msg == "cancelled":
            return
        if msg:
            self._working = False
            self.bar.hide()
            self.now.setEnabled(True)
            self.later.setText("Remind me later")
            self.status.setText(msg)
            self.status.setStyleSheet(f"color: {RED};")
            return
        self.bar.setRange(0, 0)
        self.status.setText("Installing. Grabwave will close and open again in a moment…")
        self.later.setEnabled(False)
        try:
            must_quit = self._U.install(self._path)
        except Exception as e:  # noqa: BLE001
            self._working = False
            self.bar.hide()
            self.later.setEnabled(True)
            self.later.setText("Close")
            self.status.setText(f"Couldn't start the installer: {e}")
            self.status.setStyleSheet(f"color: {RED};")
            return
        if must_quit:
            self.quitRequested.emit()
            self.accept()
        else:
            self.bar.hide()
            self.later.setEnabled(True)
            self.later.setText("Close")
            self.status.setText("The new version was opened for you. Finish the installation there.")
