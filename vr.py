"""VR video view: shows ONE eye of a stereo (side-by-side / top-bottom) VR video as a single picture that can be
dragged to look around (mouse drag = look, wheel = zoom, double-click = reset).

The picture is built on the CPU with numpy: for every output pixel a lookup into the video frame is computed once
per view change, then each video frame is just one fast gather. No OpenGL is needed, so it works everywhere."""
from __future__ import annotations

import math
import re

# key, label, projection, layout, lens angle (degrees, fisheye only)
MODES = [
    ("180sbs", "VR 180° · side by side", "eq180", "sbs", 0),
    ("180tb", "VR 180° · top / bottom", "eq180", "tb", 0),
    ("360sbs", "VR 360° · side by side", "eq360", "sbs", 0),
    ("360tb", "VR 360° · top / bottom", "eq360", "tb", 0),
    ("360mono", "VR 360° · single picture", "eq360", "mono", 0),
    ("fe190", "Fisheye 190° · side by side", "fish", "sbs", 190),
    ("fe200", "Fisheye 200° · side by side", "fish", "sbs", 200),
]
_BY_KEY = {m[0]: m for m in MODES}


def detect_mode(name: str, width: int = 0, height: int = 0) -> str:
    """Best guess of the VR layout from the file name and picture size; '' if it looks like a normal video."""
    n = re.sub(r"[^a-z0-9]+", "_", (name or "").lower())
    toks = set(n.split("_"))
    tb = bool({"tb", "ou", "topbottom", "overunder", "3dv"} & toks) or "top_bottom" in n or "over_under" in n
    sbs = bool({"lr", "sbs", "3dh", "sidebyside"} & toks) or "side_by_side" in n
    fish = "fisheye" in n or "mkx200" in n or "mkx220" in n or "fe190" in n or "fe200" in n or "rf52" in n
    f360 = "360" in toks or "_360" in n or "360vr" in n or "vr360" in n
    f180 = "180" in toks or "_180" in n or "180x180" in n or "vr180" in n
    vr = f360 or f180 or fish or "vr" in toks or "3d" in toks or tb or sbs
    if fish:
        return "fe200" if ("200" in n or "220" in n) else "fe190"
    if f360:
        return "360tb" if tb else ("360sbs" if sbs else "360mono" if (width and abs(width / max(height, 1) - 2) < 0.1 and not tb) else "360tb")
    if f180 or vr:
        return "180tb" if tb else "180sbs"
    if width >= 2880 and height > 0:  # no hint in the name: judge by the shape of the picture
        ar = width / height
        if abs(ar - 2.0) < 0.04:
            return "180sbs"
        if abs(ar - 1.0) < 0.04:
            return "360tb"
    return ""


def build_map(key: str, ow: int, oh: int, yaw: float, pitch: float, fov: float, sw: int, sh: int):
    """For each output pixel: (index into the flattened video frame, mask of pixels outside the picture)."""
    import numpy as np
    _k, _label, proj, layout, lens = _BY_KEY[key]
    t = math.tan(math.radians(fov) / 2)
    xs = ((np.arange(ow) + 0.5) / ow * 2 - 1) * t * (ow / oh)
    ys = (1 - (np.arange(oh) + 0.5) / oh * 2) * t
    x = np.broadcast_to(xs[None, :], (oh, ow)).astype(np.float32)
    y = np.broadcast_to(ys[:, None], (oh, ow)).astype(np.float32)
    z = np.ones((oh, ow), np.float32)
    cp, sp = math.cos(math.radians(pitch)), math.sin(math.radians(pitch))
    y2 = y * cp + z * sp          # positive pitch looks up
    z2 = -y * sp + z * cp
    ca, sa = math.cos(math.radians(yaw)), math.sin(math.radians(yaw))
    x3 = x * ca + z2 * sa         # positive yaw looks right
    z3 = -x * sa + z2 * ca
    n = np.sqrt(x3 * x3 + y2 * y2 + z3 * z3)
    dx, dy, dz = x3 / n, y2 / n, z3 / n

    if layout == "sbs":
        ew, eh = sw // 2, sh
    elif layout == "tb":
        ew, eh = sw, sh // 2
    else:
        ew, eh = sw, sh
    lon = np.arctan2(dx, dz)
    lat = np.arcsin(np.clip(dy, -1, 1))
    if proj == "eq360":
        u = lon / (2 * math.pi) + 0.5
        v = lat / math.pi + 0.5
        bad = np.zeros((oh, ow), bool)
    elif proj == "eq180":
        u = lon / math.pi + 0.5
        v = lat / math.pi + 0.5
        bad = (u < 0) | (u > 1)
    else:  # fisheye: distance from the lens axis is proportional to the angle
        theta = np.arccos(np.clip(dz, -1, 1))
        r = theta / math.radians(lens / 2)
        phi = np.arctan2(dy, dx)
        side = min(ew, eh)
        u = 0.5 + 0.5 * r * np.cos(phi) * (side / ew)
        v = 0.5 + 0.5 * r * np.sin(phi) * (side / eh)
        bad = r > 1
    ix = np.clip((u * ew).astype(np.int32), 0, ew - 1)
    iy = np.clip(((1 - v) * eh).astype(np.int32), 0, eh - 1)  # v is "up", image rows count from the top
    idx = (iy * sw + ix).astype(np.int32)
    idx[bad] = 0
    return idx, bad


try:
    from PySide6 import QtCore, QtGui, QtMultimedia, QtWidgets
    from PySide6.QtCore import Qt
except Exception:  # noqa: BLE001  (module still importable for tests without Qt)
    QtWidgets = None

if QtWidgets is not None:
    class VRView(QtWidgets.QWidget):
        """Video surface for QMediaPlayer.setVideoSink(view.sink) that shows one eye, looking where you drag."""

        def __init__(self, parent=None):
            super().__init__(parent)
            self.setAttribute(Qt.WA_OpaquePaintEvent, True)
            self.setMinimumSize(320, 200)
            self.sink = QtMultimedia.QVideoSink(self)
            self.sink.videoFrameChanged.connect(self._on_frame)
            self.mode = "180sbs"
            self.yaw = self.pitch = 0.0
            self.fov = 90.0
            self._arr = None      # latest frame as uint32 pixels
            self._sw = self._sh = 0
            self._map_key = None
            self._map = None
            self._out = None
            self._img = None
            self._drag = None
            self._hint = 0
            self.setCursor(Qt.OpenHandCursor)

        # --- settings ------------------------------------------------------------------------
        def set_mode(self, key: str):
            if key in _BY_KEY:
                self.mode = key
                self.yaw = self.pitch = 0.0
                self.fov = 90.0
                self._map_key = None
                self._hint = 1
                QtCore.QTimer.singleShot(5000, self._clear_hint)
                self._render()

        def _clear_hint(self):
            self._hint = 0
            self.update()

        # --- frames --------------------------------------------------------------------------
        def _on_frame(self, frame):
            if not self.isVisible():
                return
            try:
                import numpy as np
                img = frame.toImage()
                if img.isNull():
                    return
                if img.format() not in (QtGui.QImage.Format_RGB32, QtGui.QImage.Format_ARGB32,
                                        QtGui.QImage.Format_ARGB32_Premultiplied):
                    img = img.convertToFormat(QtGui.QImage.Format_RGB32)
                w, h = img.width(), img.height()
                ptr = img.constBits()
                try:
                    arr = np.frombuffer(ptr, dtype=np.uint32, count=w * h)
                except TypeError:
                    arr = np.frombuffer(ptr.asarray(w * h * 4), dtype=np.uint32, count=w * h)
                self._arr = arr.copy()  # the image's memory is only borrowed
                self._sw, self._sh = w, h
            except Exception:  # noqa: BLE001
                return
            self._render()

        def _render(self):
            if self._arr is None or self.width() < 8 or self.height() < 8:
                return
            import numpy as np
            ow = min(self.width(), 1280)
            oh = max(8, int(ow * self.height() / self.width()))
            key = (self.mode, ow, oh, round(self.yaw, 2), round(self.pitch, 2), round(self.fov, 1),
                   self._sw, self._sh)
            if key != self._map_key:
                self._map = build_map(self.mode, ow, oh, self.yaw, self.pitch, self.fov, self._sw, self._sh)
                self._map_key = key
            idx, bad = self._map
            out = np.take(self._arr, idx)
            out[bad] = 0xFF000000
            self._out = out
            self._img = QtGui.QImage(out.data, ow, oh, ow * 4, QtGui.QImage.Format_RGB32)
            self.update()

        # --- painting ------------------------------------------------------------------------
        def paintEvent(self, _e):
            p = QtGui.QPainter(self)
            p.fillRect(self.rect(), Qt.black)
            if self._img is not None:
                p.setRenderHint(QtGui.QPainter.SmoothPixmapTransform, True)
                p.drawImage(self.rect(), self._img)
            if self._hint:
                p.setPen(QtGui.QColor(255, 255, 255, 220))
                p.fillRect(QtCore.QRect(0, self.height() - 34, self.width(), 34), QtGui.QColor(0, 0, 0, 140))
                p.drawText(QtCore.QRect(0, self.height() - 34, self.width(), 34), Qt.AlignCenter,
                           "Drag to look around  ·  scroll to zoom  ·  double-click to reset")
            p.end()

        def resizeEvent(self, e):
            self._render()
            super().resizeEvent(e)

        # --- looking around ------------------------------------------------------------------
        def mousePressEvent(self, e):
            if e.button() == Qt.LeftButton:
                self._drag = e.position()
                self.setCursor(Qt.ClosedHandCursor)

        def mouseMoveEvent(self, e):
            if self._drag is None:
                return
            d = e.position() - self._drag
            self._drag = e.position()
            k = self.fov / max(self.width(), 1)  # degrees per pixel: the picture follows the pointer
            self.yaw -= d.x() * k
            self.pitch = max(-89.0, min(89.0, self.pitch + d.y() * k))
            self._render()

        def mouseReleaseEvent(self, e):
            self._drag = None
            self.setCursor(Qt.OpenHandCursor)

        def mouseDoubleClickEvent(self, e):
            self.yaw = self.pitch = 0.0
            self.fov = 90.0
            self._render()

        def wheelEvent(self, e):
            self.fov = max(30.0, min(120.0, self.fov - e.angleDelta().y() / 120 * 5))
            self._render()
