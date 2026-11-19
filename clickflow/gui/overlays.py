"""Full-screen overlays: the rectangle-capture overlay, the match highlighter and the STOP badge.

These are native top-level windows that live on somebody else's desktop, so they use the fixed ``theme.OVERLAY`` colours
(never the app theme) and only flat shapes: every stroke is drawn twice - an ink pen one pixel wider on each side, then the
coloured pen - so it stays readable over light and dark backgrounds. No shadow, no blur.
"""
from __future__ import annotations

import time
from typing import Optional

import numpy as np
from PySide6.QtCore import QPoint, QRect, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QGuiApplication, QPainter, QPen
from PySide6.QtWidgets import QWidget

from .. import platform_utils
from . import theme
from .qtutils import bgr_to_pixmap

_FONT = "Albert Sans"
_MONO = theme.MONO_FAMILY


def _ui_font(points: float, weight: "QFont.Weight" = QFont.Weight.Normal) -> QFont:
    f = QFont()
    f.setFamilies(theme.ui_families())  # (kept short: every extra family slows the first use of the font)
    f.setPointSizeF(points)
    f.setWeight(weight)
    return f


def _chip(p: QPainter, text: str, x: float, y: float, bg: QColor, fg: QColor, above: bool = True) -> None:
    """Small solid label glued to the top-left edge of a box (flips inside when it would leave the screen)."""
    font = QFont(_MONO, 9)
    font.setStyleHint(QFont.StyleHint.Monospace)
    p.save()
    p.setFont(font)
    w = QFontMetrics(font).horizontalAdvance(text) + 14
    top = y - 24 if above and y >= 26 else y + 2
    r = QRectF(x, top, w, 22)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(bg)
    p.drawRoundedRect(r, 5, 5)
    p.setPen(fg)
    p.drawText(r, Qt.AlignmentFlag.AlignCenter, text)
    p.restore()


def _cased_rect(p: QPainter, rect: QRectF, color: QColor, width: float, dashed: bool = False) -> None:
    """Rectangle outline with an ink casing: ink pen (width + 2) first, then the coloured pen."""
    style = Qt.PenStyle.DashLine if dashed else Qt.PenStyle.SolidLine
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.setPen(QPen(theme.overlay("ov_ink"), width + 2, style))
    p.drawRect(rect)
    p.setPen(QPen(color, width, style))
    p.drawRect(rect)


class CaptureOverlay(QWidget):
    """Shows a dimmed screenshot; the user drags a rectangle.

    Emits ``selected(x, y, w, h)`` in *screenshot pixels* (not Qt logical units), so the
    crop is exact on HiDPI screens. Esc or a right click cancels. ``kind`` is ``"target"`` (cream selection) or
    ``"area"`` (teal selection: a search area).
    """

    selected = Signal(int, int, int, int)
    cancelled = Signal()

    def __init__(self, image: np.ndarray, geometry: QRect, hint: str,
                 guides: Optional[list[tuple[int, int, int, int, str]]] = None, kind: str = "target") -> None:
        super().__init__(None)
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
                            | Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setGeometry(geometry)
        self._pix = bgr_to_pixmap(image)
        self._hint = hint
        self._kind = kind
        # (x, y, w, h in screenshot pixels, label): existing boxes / the template, drawn for orientation
        self._guides = list(guides or [])
        self._start: Optional[QPoint] = None
        self._cur: Optional[QPoint] = None
        self._done = False

    def open(self) -> None:
        self.show()
        self.raise_()
        self.activateWindow()
        self.setFocus()

    def _sel(self) -> Optional[QRect]:
        if self._start is None or self._cur is None:
            return None
        x0, x1 = sorted((self._start.x(), self._cur.x()))
        y0, y1 = sorted((self._start.y(), self._cur.y()))
        return QRect(x0, y0, x1 - x0, y1 - y0)  # not QRect(p1, p2): that one is inclusive (+1 px)

    def _color(self) -> QColor:
        return theme.overlay("ov_area" if self._kind == "area" else "ov_target")

    def paintEvent(self, _e) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        p.drawPixmap(self.rect(), self._pix)
        dim = theme.overlay("ov_dim")
        sel = self._sel()
        if sel and sel.width() > 0 and sel.height() > 0:
            # the dimmer is four rectangles around the selection: the hole keeps the screenshot's own pixels
            w, h = self.width(), self.height()
            p.fillRect(QRect(0, 0, w, sel.top()), dim)
            p.fillRect(QRect(0, sel.bottom() + 1, w, h - sel.bottom() - 1), dim)
            p.fillRect(QRect(0, sel.top(), sel.left(), sel.height()), dim)
            p.fillRect(QRect(sel.right() + 1, sel.top(), w - sel.right() - 1, sel.height()), dim)
            sx, sy = self._pix.width() / self.width(), self._pix.height() / self.height()
            _cased_rect(p, QRectF(sel), self._color(), 2)
            # 8 x 8 square handles on the corners and edge midpoints, 1 px ink border
            ink, fill = theme.overlay("ov_ink"), theme.overlay("ov_target")
            p.setBrush(fill)
            p.setPen(QPen(ink, 1))
            for hx, hy in ((sel.left(), sel.top()), (sel.center().x(), sel.top()), (sel.right(), sel.top()),
                           (sel.left(), sel.center().y()), (sel.right(), sel.center().y()),
                           (sel.left(), sel.bottom()), (sel.center().x(), sel.bottom()), (sel.right(), sel.bottom())):
                p.drawRect(QRectF(hx - 4, hy - 4, 8, 8))
            _chip(p, f"{round(sel.width() * sx)} × {round(sel.height() * sy)} px", sel.x(), sel.y(),
                  theme.overlay("ov_bar"), theme.overlay("ov_bar_text"), above=True)
        else:
            p.fillRect(self.rect(), dim)
        self._paint_guides(p)
        # hint bar (top-left)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        font = _ui_font(13)
        p.setFont(font)
        w = QFontMetrics(font).horizontalAdvance(self._hint) + 32
        bar = QRectF(20, 20, w, 36)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(theme.overlay("ov_bar"))
        p.drawRoundedRect(bar, 18, 18)
        p.setPen(theme.overlay("ov_bar_text"))
        p.drawText(bar, Qt.AlignmentFlag.AlignCenter, self._hint)
        p.end()

    def _paint_guides(self, p: QPainter) -> None:
        if not self._guides:
            return
        sx, sy = self._pix.width() / self.width(), self._pix.height() / self.height()
        for x, y, w, h, label in self._guides:
            r = QRectF(x / sx, y / sy, w / sx, h / sy)
            col = theme.overlay("ov_target" if label == "template" else "ov_area")
            _cased_rect(p, r, col, 2, dashed=True)
            _chip(p, label, r.x(), r.y(), theme.overlay("ov_bar"), theme.overlay("ov_bar_text"), above=True)

    def mousePressEvent(self, e) -> None:  # noqa: N802
        if e.button() == Qt.MouseButton.RightButton:
            self._finish(None)
            return
        self._start = self._cur = e.position().toPoint()
        self.update()

    def mouseMoveEvent(self, e) -> None:  # noqa: N802
        if self._start is not None:
            self._cur = e.position().toPoint()
            self.update()

    def mouseReleaseEvent(self, e) -> None:  # noqa: N802
        if self._start is None or e.button() != Qt.MouseButton.LeftButton:
            return
        self._cur = e.position().toPoint()
        sel = self._sel()
        self._start = None
        if not sel or sel.width() < 4 or sel.height() < 4:
            self.update()
            return  # too small: let the user drag again
        sx, sy = self._pix.width() / self.width(), self._pix.height() / self.height()
        rect = (round(sel.x() * sx), round(sel.y() * sy), round(sel.width() * sx), round(sel.height() * sy))
        self._finish(rect)

    def keyPressEvent(self, e) -> None:  # noqa: N802
        if e.key() == Qt.Key.Key_Escape:
            self._finish(None)

    def closeEvent(self, e) -> None:  # noqa: N802
        if not self._done:
            self._done = True
            self.cancelled.emit()
        super().closeEvent(e)

    def _finish(self, rect) -> None:
        if self._done:
            return
        self._done = True
        if rect is None:
            self.cancelled.emit()
        else:
            self.selected.emit(*rect)
        self.close()


class Highlighter(QWidget):
    """Click-through, always-on-top layer that draws boxes around matches for a moment."""

    def __init__(self) -> None:
        super().__init__(None)
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
                            | Qt.WindowType.Tool | Qt.WindowType.WindowTransparentForInput
                            | Qt.WindowType.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._boxes: list[tuple[QRectF, str, QColor, float, bool]] = []  # rect, label, colour, expiry, dashed
        self._timer = QTimer(self)
        self._timer.setInterval(60)
        self._timer.timeout.connect(self._tick)

    def flash(self, rect: tuple[float, float, float, float], label: str, found: bool, ms: int = 700,
              kind: str = "match") -> None:
        """``rect`` is (left, top, width, height) in *input* (screen) coordinates.

        ``kind="region"`` draws a dashed teal search area instead of a green (found) / amber (candidate) match box.
        """
        screen = QGuiApplication.primaryScreen()
        geo = screen.virtualGeometry()
        f = platform_utils.input_to_qt_factor(screen.devicePixelRatio())
        l, t, w, h = rect
        box = QRectF(l * f - geo.x(), t * f - geo.y(), w * f, h * f)
        color = theme.overlay("ov_area" if kind == "region" else ("ov_found" if found else "ov_cand"))
        self._boxes.append((box, label, color, time.monotonic() + ms / 1000.0, kind == "region"))
        if not self.isVisible():
            self.setGeometry(geo)
            self.show()
        self._timer.start()
        self.update()

    def _tick(self) -> None:
        now = time.monotonic()
        self._boxes = [b for b in self._boxes if b[3] > now]
        if not self._boxes:
            self._timer.stop()
            self.hide()
        else:
            self.update()

    def paintEvent(self, _e) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        for box, label, color, _, dashed in self._boxes:
            outer = box.adjusted(-4, -4, 4, 4)  # drawn outside the match so the target pixels stay untouched
            _cased_rect(p, outer, color, 2 if dashed else 3, dashed)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            _chip(p, label, outer.x(), outer.y(), color, theme.overlay("ov_ink"), above=True)
            p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        p.end()


class StopBadge(QWidget):
    """The always-on-top STOP badge shown while a run is active. Click anywhere on it to stop.

    Left block: the red "STOP" button. Right block: live status text ("Running - 37 clicks - F8"), refreshed at most twice a
    second. When the run ends it turns into a result badge that stays for 4 seconds (a click hides it at once). It never
    takes keyboard focus, so clicking it does not steal focus from the window being automated.
    """

    clicked = Signal()
    HEIGHT = 44
    RESULT_MS = 4000

    def __init__(self) -> None:
        super().__init__(None)
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
                            | Qt.WindowType.Tool | Qt.WindowType.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._label = "STOP"           # the left block's word
        self._left = theme.overlay("ov_stop")
        self._status = ""              # the right block's text
        self._hotkey = ""
        self._result = False
        self._last_update = 0.0
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self.hide)
        self._relayout()

    # ------------------------------------------------------------------ API

    def show_badge(self, hotkey: str) -> None:
        """Run started: red STOP block, status text empty until the first update."""
        self._hide_timer.stop()
        self._result = False
        self._hotkey = hotkey
        self._label, self._left = "STOP", theme.overlay("ov_stop")
        self._status = hotkey
        self._last_update = 0.0
        self._place_and_show()

    def set_status(self, text: str) -> None:
        """Live status ("Running · 37 clicks"); the hotkey is appended. Throttled to two refreshes a second."""
        if self._result:
            return
        now = time.monotonic()
        if now - self._last_update < 0.5:
            return
        self._last_update = now
        status = f"{text} · {self._hotkey}" if self._hotkey else text
        if status != self._status:
            self._status = status
            self._place_and_show()

    def show_result(self, kind: str, text: str) -> None:
        """Run finished: ``kind`` is done | sto | err; the badge stays for 4 s and then hides."""
        self._result = True
        word, col = {"done": ("DONE", "ov_found"), "err": ("FAILED", "ov_stop")}.get(kind, ("STOPPED", "ov_cand"))
        self._label, self._left = word, theme.overlay(col)
        self._status = text
        self._place_and_show()
        self._hide_timer.start(self.RESULT_MS)

    def text(self) -> str:
        return f"{self._label} {self._status}".strip()

    # ------------------------------------------------------------------ geometry and painting

    def _fonts(self) -> tuple[QFont, QFont]:
        bold = _ui_font(11, QFont.Weight.Medium)  # nothing in the interface is bold
        body = _ui_font(11)
        return bold, body

    def _blocks(self) -> tuple[float, float]:
        bold, body = self._fonts()
        left = 14 + 12 + 8 + QFontMetrics(bold).horizontalAdvance(self._label) + 16 + 4
        right = 16 + QFontMetrics(body).horizontalAdvance(self._status) + 18 if self._status else 0
        return left, right

    def _relayout(self) -> None:
        left, right = self._blocks()
        self.setFixedSize(int(left + right + 8), self.HEIGHT)  # + the two rings (2 px ink + 2 px cream) on each side

    def _place_and_show(self) -> None:
        self._relayout()
        geo = QGuiApplication.primaryScreen().geometry()
        self.move(geo.x() + (geo.width() - self.width()) // 2, geo.y() + 6)
        self.show()
        self.raise_()
        self.update()

    def paintEvent(self, _e) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        ink, cream = theme.overlay("ov_ink"), theme.overlay("ov_target")
        full = QRectF(self.rect())
        # paint order: ink ring, cream ring, then the red block (left) and the dark block (right)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(ink)
        p.drawRoundedRect(full, self.HEIGHT / 2, self.HEIGHT / 2)
        p.setBrush(cream)
        p.drawRoundedRect(full.adjusted(2, 2, -2, -2), self.HEIGHT / 2 - 2, self.HEIGHT / 2 - 2)
        inner = full.adjusted(4, 4, -4, -4)
        left_w, right_w = self._blocks()
        lw = left_w
        rad = inner.height() / 2
        right_rect = QRectF(inner.left() + lw - rad, inner.top(), inner.width() - lw + rad, inner.height())
        if self._status:
            p.setBrush(theme.overlay("ov_bar"))
            p.drawRoundedRect(right_rect, rad, rad)
        p.setBrush(self._left)
        p.drawRoundedRect(QRectF(inner.left(), inner.top(), lw, inner.height()), rad, rad)
        bold, body = self._fonts()
        white = QColor("#ffffff")
        ink_on_left = self._left in (theme.overlay("ov_found"), theme.overlay("ov_cand"))
        fg = ink if ink_on_left else white
        p.setFont(bold)
        fm = QFontMetrics(bold)
        x0 = inner.left() + 14
        p.setPen(QPen(fg, 2))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(QRectF(x0 + 1, inner.center().y() - 6, 12, 12), 2.5, 2.5)  # the "stop" square glyph
        p.setPen(fg)
        p.drawText(QRectF(x0 + 12 + 8 + 1, inner.top(), fm.horizontalAdvance(self._label) + 4, inner.height()),
                   Qt.AlignmentFlag.AlignVCenter, self._label)
        if self._status:
            p.setFont(body)
            p.setPen(theme.overlay("ov_bar_text"))
            p.drawText(QRectF(inner.left() + lw + 8, inner.top(), right_w, inner.height()), Qt.AlignmentFlag.AlignVCenter,
                       self._status)
        p.end()

    def mousePressEvent(self, e) -> None:  # noqa: N802
        if e.button() != Qt.MouseButton.LeftButton:
            return
        if self._result:
            self._hide_timer.stop()
            self.hide()
        else:
            self.clicked.emit()
