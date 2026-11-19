"""Input widgets and the small custom controls of the ClickFlow design system.

Wheel safety: combo boxes, spin boxes and sliders ignore the mouse wheel unless they have keyboard focus.
Qt's default (scrolling over a combo box *changes its value*) silently flips settings (e.g. "Left click" ->
"Right click") while the user is only scrolling the page.

Custom controls (Switch, Segmented, Pill, Alert, Group, ThresholdSlider, HotkeyEdit, ...) read their colours from
``theme.tok`` at paint time, so a repaint is all a restyle needs.
"""
from __future__ import annotations

import time
import weakref
from typing import Callable, Optional

from PySide6.QtCore import QEasingCurve, QPointF, QRect, QRectF, QSize, Qt, QTimer, QVariantAnimation, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (
    QAbstractButton, QButtonGroup, QComboBox, QDoubleSpinBox, QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QPushButton, QSizePolicy, QSlider, QSpinBox, QStyle, QStyledItemDelegate, QToolButton, QVBoxLayout, QWidget,
)

from . import icons, theme
from .i18n import tr

# ============================================================================ wheel-safe inputs


class _WheelGuard:
    def _guard_init(self) -> None:
        # Click or Tab gives focus; the wheel alone never does.
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def wheelEvent(self, event) -> None:  # noqa: N802 (Qt API)
        if self.hasFocus():
            super().wheelEvent(event)
        else:
            event.ignore()  # lets the parent (the scroll area) handle it


class SafeComboBox(_WheelGuard, QComboBox):
    def __init__(self, *a, **k) -> None:
        super().__init__(*a, **k)
        self._guard_init()


class SafeSpinBox(_WheelGuard, QSpinBox):
    def __init__(self, *a, **k) -> None:
        super().__init__(*a, **k)
        self._guard_init()


class SafeDoubleSpinBox(_WheelGuard, QDoubleSpinBox):
    def __init__(self, *a, **k) -> None:
        super().__init__(*a, **k)
        self._guard_init()


class SafeSlider(_WheelGuard, QSlider):
    def __init__(self, *a, **k) -> None:
        super().__init__(*a, **k)
        self._guard_init()


# ============================================================================ theme-aware icons

_themed: "weakref.WeakSet" = weakref.WeakSet()


def _refresh_themed() -> None:
    for w in list(_themed):
        try:
            w.refresh_theme()
        except RuntimeError:  # the C++ object is already gone
            pass


theme.on_change(_refresh_themed)


class _IconSlot:
    """A button's icon that is re-rendered when the theme changes."""

    def __init__(self, btn: QAbstractButton, name: str, token: str, size: int, stroke: float) -> None:
        self.btn, self.name, self.token, self.size, self.stroke = btn, name, token, size, stroke

    def apply(self) -> None:
        self.btn.setIcon(icons.icon(self.name, theme.tok(self.token), self.size, self.stroke))
        self.btn.setIconSize(QSize(self.size, self.size))


class _Refresher:
    def __init__(self) -> None:
        self.slots: "weakref.WeakKeyDictionary[QAbstractButton, _IconSlot]" = weakref.WeakKeyDictionary()

    def refresh_theme(self) -> None:
        for s in list(self.slots.values()):
            try:
                s.apply()
            except RuntimeError:
                pass


_icon_refresher = _Refresher()
_themed.add(_icon_refresher)


def set_icon(btn: QAbstractButton, name: str, token: str = "tx", size: int = 16, stroke: float = 1.5) -> None:
    slot = _IconSlot(btn, name, token, size, stroke)
    _icon_refresher.slots[btn] = slot
    slot.apply()


_VARIANT_ICON_TOKEN = {"primary": "on_ac", "danger": "on_dg", "ghost": "tx2", "link": "ac_tx", "": "tx"}


def button(text: str = "", icon: str = "", variant: str = "", size: str = "", tip: str = "",
           on_click: Optional[Callable] = None) -> QPushButton:
    """A design-system push button. ``variant``: "" | primary | danger | ghost | link; ``size``: "" | sm | lg (md is 36 px)."""
    b = QPushButton(text)
    if variant:
        b.setProperty("variant", variant)
    if size:
        b.setProperty("size", size)
    if icon:
        set_icon(b, icon, _VARIANT_ICON_TOKEN.get(variant, "tx"), 18 if size == "lg" else 16)
    if tip:
        b.setToolTip(tip)
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    if on_click:
        b.clicked.connect(on_click)
    return b


def icon_button(icon: str, tip: str = "", variant: str = "ghost", size: str = "", on_click: Optional[Callable] = None,
                token: str = "") -> QToolButton:
    b = QToolButton()
    b.setProperty("btn", True)
    b.setProperty("icononly", True)
    if variant:
        b.setProperty("variant", variant)
    if size:
        b.setProperty("size", size)
    set_icon(b, icon, token or _VARIANT_ICON_TOKEN.get(variant, "tx"), 16)
    b.setToolTip(tip)
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    b.setFocusPolicy(Qt.FocusPolicy.TabFocus)
    if on_click:
        b.clicked.connect(on_click)
    return b


def label(text: str = "", role: str = "", wrap: bool = False) -> QLabel:
    lb = QLabel(text)
    if role:
        lb.setProperty("role", role)
    if wrap:
        lb.setWordWrap(True)
    return lb


def tag(text: str, tone: str = "") -> QLabel:
    lb = QLabel(text)
    lb.setProperty("tag", tone)
    return lb


def repolish(w: QWidget) -> None:
    w.style().unpolish(w)
    w.style().polish(w)
    w.update()


def hline() -> QFrame:
    f = QFrame()
    f.setProperty("sep", True)
    f.setFixedHeight(1)
    return f


# ============================================================================ switch

SWITCH_MS = 170  # how long the thumb takes to slide across


def _ease(t: float) -> float:
    """Smooth start and end (smoothstep), so the thumb glides like a native toggle."""
    t = max(0.0, min(1.0, t))
    return t * t * (3 - 2 * t)


def paint_switch(p: QPainter, r: QRect, on: bool, enabled: bool = True, t: Optional[float] = None) -> None:
    """Draw a 36x20 switch. ``t`` (0 = off, 1 = on) is the animation position; without it the switch is fully on or off."""
    if t is None:
        t = 1.0 if on else 0.0
    e = _ease(t)
    p.save()
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    if not enabled:
        p.setOpacity(0.5)
    p.setPen(Qt.PenStyle.NoPen)
    off, on_c = theme.qcolor("sw_off"), theme.qcolor("sw_on")  # ink when on, grey when off (coral is kept for Start)
    p.setBrush(QColor(*(round(off.getRgb()[i] + (on_c.getRgb()[i] - off.getRgb()[i]) * e) for i in range(4))))
    p.drawRoundedRect(QRectF(r), r.height() / 2, r.height() / 2)
    d = r.height() - 4
    x0, x1 = r.left() + 2, r.right() - d - 1
    p.setBrush(theme.qcolor("sw_th"))
    p.drawEllipse(QRectF(x0 + (x1 - x0) * e, r.top() + 2, d, d))
    p.restore()


class Switch(QAbstractButton):
    """36x20 toggle. Same API as a QCheckBox (isChecked / setChecked / toggled).

    A click or key press slides the thumb across; clicking again mid-slide simply reverses it from where it is, so quick
    repeated clicks are never dropped. Changes made from code (loading another step, ...) jump straight to the new state.
    """

    def __init__(self, checked: bool = False) -> None:
        super().__init__()
        self._t = 1.0 if checked else 0.0
        self._by_user = False
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(SWITCH_MS)
        self._anim.setEasingCurve(QEasingCurve.Type.Linear)  # paint_switch eases the position
        self._anim.valueChanged.connect(self._set_t)
        self.setCheckable(True)
        self.setChecked(checked)
        self.toggled.connect(self._on_toggled)
        self.setFixedSize(36, 20)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(36, 20)

    def nextCheckState(self) -> None:  # noqa: N802 (Qt API): runs for a click or key press, not for setChecked()
        self._by_user = True
        try:
            super().nextCheckState()
        finally:
            self._by_user = False

    def _set_t(self, value) -> None:
        self._t = float(value)
        self.update()

    def _on_toggled(self, on: bool) -> None:
        target = 1.0 if on else 0.0
        self._anim.stop()
        if self._by_user and self.isVisible():
            self._anim.setStartValue(self._t)  # from wherever the thumb is now
            self._anim.setEndValue(target)
            self._anim.setDuration(max(60, round(SWITCH_MS * abs(target - self._t))))
            self._anim.start()
        else:
            self._set_t(target)

    def paintEvent(self, _e) -> None:  # noqa: N802
        p = QPainter(self)
        paint_switch(p, self.rect(), self.isChecked(), self.isEnabled(), self._t)
        if self.hasFocus():
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            p.setPen(QPen(theme.qcolor("fc"), 1.5))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 10, 10)
        p.end()


class SwitchRow(QWidget):
    """Switch + bold title (+ small grey subtitle). ``.switch`` is the underlying toggle."""

    def __init__(self, title: str, sub: str = "") -> None:
        super().__init__()
        self.switch = Switch()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)
        lay.addWidget(self.switch, 0, Qt.AlignmentFlag.AlignTop if sub else Qt.AlignmentFlag.AlignVCenter)
        col = QVBoxLayout()
        col.setSpacing(0)
        self.title = label(title, "section")
        self.title.setMinimumHeight(20)  # the same height as the 20 px switch, so the first line sits on its centre
        col.addWidget(self.title)
        self.sub = label(sub, "hint")
        col.addWidget(self.sub)
        lay.addLayout(col, 1)
        self.sub.setVisible(bool(sub))  # only now has the label a parent: showing a parentless widget flashes a window
        self.title.mousePressEvent = lambda e: self.switch.click() if self.switch.isEnabled() else None  # (click() animates)


# ============================================================================ segmented control

class Segmented(QFrame):
    """Pill-shaped single-choice control. Mirrors the part of QComboBox the editors use
    (addItem / currentData / setCurrentIndex / findData / currentIndexChanged)."""

    currentIndexChanged = Signal(int)

    def __init__(self, fill: bool = True) -> None:
        super().__init__()
        self.setProperty("seg", True)
        self._fill = fill
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self._btns: list[QToolButton] = []
        self._data: list[object] = []
        self._current = -1
        self.setFixedHeight(36)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(3, 3, 3, 3)
        lay.setSpacing(2)
        self._lay = lay

    def addItem(self, text: str, data: object = None) -> None:  # noqa: N802
        b = QToolButton()
        b.setProperty("seg", True)
        b.setText(text)
        b.setCheckable(True)
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        b.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        if self._fill:
            b.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        i = len(self._btns)
        self._btns.append(b)
        self._data.append(data)
        self._group.addButton(b, i)
        b.clicked.connect(lambda _=False, idx=i: self.setCurrentIndex(idx))
        self._lay.addWidget(b, 1 if self._fill else 0)
        if self._current < 0:
            self.setCurrentIndex(0)

    def setItemText(self, i: int, text: str) -> None:  # noqa: N802
        self._btns[i].setText(text)

    def count(self) -> int:
        return len(self._btns)

    def currentIndex(self) -> int:  # noqa: N802
        return self._current

    def currentData(self) -> object:  # noqa: N802
        return self._data[self._current] if 0 <= self._current < len(self._data) else None

    def findData(self, data: object) -> int:  # noqa: N802
        return self._data.index(data) if data in self._data else -1

    def setCurrentIndex(self, i: int) -> None:  # noqa: N802
        if not (0 <= i < len(self._btns)):
            return
        changed = i != self._current
        self._current = i
        for k, b in enumerate(self._btns):
            b.setChecked(k == i)
        if changed:
            self.currentIndexChanged.emit(i)

    def setCurrentData(self, data: object) -> None:  # noqa: N802
        self.setCurrentIndex(max(0, self.findData(data)))

    def clearSelection(self) -> None:  # noqa: N802
        """No segment selected (e.g. the speed slider sits between the presets)."""
        if self._current < 0:
            return
        self._current = -1
        self._group.setExclusive(False)
        for b in self._btns:
            b.setChecked(False)
        self._group.setExclusive(True)


class TabStrip(QFrame):
    """Underlined tabs (Step settings | Global settings)."""

    currentChanged = Signal(int)

    def __init__(self) -> None:
        super().__init__()
        self.setProperty("bare", True)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        self._lay = lay
        self._group = QButtonGroup(self)
        self._btns: list[QToolButton] = []
        self._cur = -1

    def add(self, text: str, extra: QWidget | None = None) -> QToolButton:
        b = QToolButton()
        b.setProperty("tab", True)
        b.setText(text.replace("&", "&&"))
        b.setCheckable(True)
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        i = len(self._btns)
        self._btns.append(b)
        self._group.addButton(b, i)
        b.clicked.connect(lambda _=False, idx=i: self.setCurrentIndex(idx))
        self._lay.addWidget(b)
        if extra is not None:
            self._lay.addWidget(extra)
        if self._cur < 0:
            self.setCurrentIndex(0)
        return b

    def finish(self) -> None:
        self._lay.addStretch(1)

    def currentIndex(self) -> int:  # noqa: N802
        return self._cur

    def setCurrentIndex(self, i: int) -> None:  # noqa: N802
        if not (0 <= i < len(self._btns)):
            return
        self._cur = i
        for k, b in enumerate(self._btns):
            b.setChecked(k == i)
        self.currentChanged.emit(i)

    def setTabText(self, i: int, text: str) -> None:  # noqa: N802
        self._btns[i].setText(text.replace("&", "&&"))


# ============================================================================ status, alert, tile, meter

# kind -> status glyph in the icon sprite. State is always glyph shape + a word, never colour alone.
_STATUS_GLYPH = {"idle": "idle", "cdn": "counting", "run": "running", "dry": "counting", "sto": "stopped",
                 "err": "fail", "done": "done", "warn": "warn"}


class Pill(QWidget):
    """Status: a glyph shape and a word. kind = idle | cdn (countdown) | run | dry | sto (stopped) | done | err | warn.

    ``big`` is the 40 px status word of the run dashboard; the small one lives in the bottom bar.
    """

    def __init__(self, kind: str = "idle", text: str = "", big: bool = False) -> None:
        super().__init__()
        self._kind, self._text, self._big = kind, text, big
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

    def set_state(self, kind: str, text: str) -> None:
        self._kind, self._text = kind, text
        self.updateGeometry()
        self.update()

    def kind(self) -> str:
        return self._kind

    def text(self) -> str:
        return self._text

    def _font(self) -> QFont:
        f = QFont(self.font())
        f.setPixelSize(40 if self._big else 14)
        f.setWeight(theme.semibold())
        return f

    def _glyph(self) -> int:
        return 28 if self._big else 16

    def sizeHint(self) -> QSize:  # noqa: N802
        w = QFontMetrics(self._font()).horizontalAdvance(self._text)
        gap = 14 if self._big else 8
        return QSize(self._glyph() + gap + w + 2, 48 if self._big else 24)

    def paintEvent(self, _e) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        g = self._glyph()
        glyph = icons.status_pixmap(_STATUS_GLYPH.get(self._kind, "idle"), g, 2.0)
        p.drawPixmap(0, int((self.height() - g) / 2), glyph)
        p.setPen(theme.qcolor("tx"))
        p.setFont(self._font())
        gap = 14 if self._big else 8
        p.drawText(QRectF(g + gap, 0, self.width() - g - gap, self.height()), Qt.AlignmentFlag.AlignVCenter, self._text)
        p.end()


_ALERT_GLYPH = {"info": "info", "ok": "done", "wn": "warn", "er": "fail"}


class Alert(QFrame):
    """Note bar: tone is info | ok | wn | er; the tone is a glyph shape, not only a colour. Optional link action on the right.

    ``plain=True`` is the one-line hint without background or glyph (e.g. the search-area line under the target).
    """

    def __init__(self, text: str = "", tone: str = "info", action: str = "", plain: bool = False) -> None:
        super().__init__()
        self._tone = tone
        self._plain = plain
        self.setProperty("alert", tone)
        self.setProperty("plain", plain)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0 if plain else 12, 0 if plain else 10, 0 if plain else 12, 0 if plain else 10)
        lay.setSpacing(10)
        self.icon = QLabel()
        self.icon.setFixedSize(16, 16)
        lay.addWidget(self.icon, 0, Qt.AlignmentFlag.AlignTop)
        self.icon.setVisible(not plain)  # (after addWidget: a parentless widget that is shown flashes up as its own window)
        self.label = QLabel(text)
        self.label.setWordWrap(not plain)
        self.label.setProperty("role", "hint" if plain else "")
        if not plain:
            self.label.setStyleSheet("font-size: 13px;")
        lay.addWidget(self.label, 1)
        self.action = button(action, variant="link")
        lay.addWidget(self.action, 0, Qt.AlignmentFlag.AlignVCenter if plain else Qt.AlignmentFlag.AlignTop)
        self.action.setVisible(bool(action))
        _themed.add(self)
        self.refresh_theme()

    def set_tone(self, tone: str) -> None:
        self._tone = tone
        self.setProperty("alert", tone)
        repolish(self)
        self.refresh_theme()

    def set_text(self, text: str) -> None:
        self.label.setText(text)

    def text(self) -> str:
        return self.label.text()

    def tone(self) -> str:
        return self._tone

    def refresh_theme(self) -> None:
        self.icon.setPixmap(icons.status_pixmap(_ALERT_GLYPH.get(self._tone, "info"), 16, 2.0))


class Tile(QFrame):
    """Dashboard figure: small caption, big value (32 px), small footnote. No box: hierarchy comes from size."""

    def __init__(self, caption: str) -> None:
        super().__init__()
        self.setProperty("tile", True)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.k = label(caption, "field")
        self.n = label("0", "num")
        self.s = label("", "hint")
        lay.addWidget(self.k)
        lay.addWidget(self.n)
        lay.addWidget(self.s)

    def set(self, value: str, note: str = "") -> None:
        self.n.setText(value)
        self.s.setText(note)


class MatchMeter(QWidget):
    """Confidence bar 0.50..1.00 with a tick at the threshold."""

    def __init__(self) -> None:
        super().__init__()
        self._v, self._thr = 0.0, 0.85
        self.setFixedHeight(14)

    def set(self, value: float, threshold: float) -> None:
        self._v, self._thr = value, threshold
        self.update()

    @staticmethod
    def _pos(v: float) -> float:
        return max(0.0, min(1.0, (v - 0.5) / 0.5))

    def paintEvent(self, _e) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(0, 4, self.width(), 6)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(theme.qcolor("pr"))
        p.drawRoundedRect(r, 3, 3)
        w = r.width() * self._pos(self._v)
        if w > 1:
            p.setBrush(theme.qcolor("ok" if self._v >= self._thr else "wn"))
            p.drawRoundedRect(QRectF(r.left(), r.top(), w, r.height()), 3, 3)
        x = r.left() + r.width() * self._pos(self._thr)
        p.setPen(QPen(theme.qcolor("tx"), 2))
        p.drawLine(QPointF(x, 1), QPointF(x, 13))
        p.end()


# ============================================================================ collapsible group

class Group(QFrame):
    """Card with a clickable header (chevron, title, optional tag, optional right-hand widget) and a collapsible body."""

    toggled_open = Signal(bool)

    def __init__(self, title: str, open_: bool = True, right: QWidget | None = None, flat: bool = False) -> None:
        super().__init__()
        if flat:  # no box: a plain section of a page
            self.setProperty("bare", True)
        else:
            self.setProperty("card", True)
        self._open = open_
        root = QVBoxLayout(self)
        root.setContentsMargins(0 if flat else 6, 2, 0 if flat else 10, 0)
        root.setSpacing(0)
        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        head.setSpacing(8)
        self.head_btn = QToolButton()
        self.head_btn.setProperty("grouphead", True)
        self.head_btn.setText("  " + title.replace("&", "&&"))
        self.head_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.head_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.head_btn.setIconSize(QSize(16, 16))
        self.head_btn.clicked.connect(lambda: self.set_open(not self._open))
        head.addWidget(self.head_btn)
        self.tag = tag("", "ok")
        self.tag.setVisible(False)
        head.addWidget(self.tag)
        head.addStretch(1)
        self.right = right
        if right is not None:
            head.addWidget(right)
        root.addLayout(head)
        self.body = QWidget()
        self.body_lay = QVBoxLayout(self.body)
        self.body_lay.setContentsMargins(6 if flat else 10, 2, 0 if flat else 6, 8 if flat else 16)
        self.body_lay.setSpacing(12)
        root.addWidget(self.body)
        self.body.setVisible(open_)
        _themed.add(self)
        self.refresh_theme()

    def set_title(self, title: str) -> None:
        self.head_btn.setText("  " + title.replace("&", "&&"))

    def set_tag(self, text: str = "", tone: str = "ok") -> None:
        self.tag.setVisible(bool(text))
        self.tag.setText(text)
        self.tag.setProperty("tag", tone)
        repolish(self.tag)

    def is_open(self) -> bool:
        return self._open

    def set_open(self, on: bool) -> None:
        self._open = on
        self.body.setVisible(on)
        self.refresh_theme()
        self.toggled_open.emit(on)

    def refresh_theme(self) -> None:
        self.head_btn.setIcon(icons.icon("chevd" if self._open else "chevr", theme.tok("tx3"), 16, 1.75))

    def add(self, w: QWidget | QHBoxLayout | QVBoxLayout) -> None:
        if isinstance(w, QWidget):
            self.body_lay.addWidget(w)
        else:
            self.body_lay.addLayout(w)


def field(caption: str, widget: QWidget, hint: str = "") -> QWidget:
    """Caption above a control (the design's ``.fld``)."""
    w = QWidget()
    lay = QVBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(6)
    cap = label(caption, "field")
    lay.addWidget(cap)
    lay.addWidget(widget)
    if hint:
        lay.addWidget(label(hint, "hint", True))
    w.caption = cap  # type: ignore[attr-defined]
    return w


# ============================================================================ threshold slider (0.50 .. 1.00)

class ThresholdSlider(QWidget):
    """Match-strictness slider with a float API: ``value()`` / ``setValue(0.93)`` / ``valueChanged(float)``."""

    valueChanged = Signal(float)

    def __init__(self, title: str = "", lo: float = 0.5, hi: float = 1.0) -> None:
        super().__init__()
        self._lo, self._hi = lo, hi
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        self.title = label(title, "field")
        self.readout = label("0.85", "mono")
        self.readout.setStyleSheet(f"font-weight: {theme.semi_css()};")
        top.addWidget(self.title)
        top.addStretch(1)
        top.addWidget(self.readout)
        lay.addLayout(top)
        row = QHBoxLayout()
        row.setSpacing(10)
        self.low = label(tr("Loose"), "hint")
        self.high = label(tr("Strict"), "hint")
        self.slider = SafeSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(int(round(lo * 100)), int(round(hi * 100)))
        self.slider.setSingleStep(1)
        self.slider.setPageStep(5)
        self.slider.valueChanged.connect(self._changed)
        row.addWidget(self.low)
        row.addWidget(self.slider, 1)
        row.addWidget(self.high)
        lay.addLayout(row)
        self.setValue(0.85)

    def _changed(self, v: int) -> None:
        self.readout.setText(f"{v / 100:.2f}")
        self.valueChanged.emit(v / 100)

    def value(self) -> float:
        return self.slider.value() / 100

    def setValue(self, v: float) -> None:  # noqa: N802
        self.slider.setValue(int(round(max(self._lo, min(self._hi, v)) * 100)))
        self.readout.setText(f"{self.value():.2f}")

    def setToolTip(self, text: str) -> None:  # noqa: N802
        super().setToolTip(text)
        self.slider.setToolTip(text)


# ============================================================================ hotkey capture field

_QT_NAMES = {
    Qt.Key.Key_Space: "space", Qt.Key.Key_Return: "enter", Qt.Key.Key_Enter: "enter", Qt.Key.Key_Tab: "tab",
    Qt.Key.Key_Backspace: "backspace", Qt.Key.Key_Delete: "delete", Qt.Key.Key_Home: "home", Qt.Key.Key_End: "end",
    Qt.Key.Key_PageUp: "pageup", Qt.Key.Key_PageDown: "pagedown", Qt.Key.Key_Up: "up", Qt.Key.Key_Down: "down",
    Qt.Key.Key_Left: "left", Qt.Key.Key_Right: "right", Qt.Key.Key_Insert: "insert", Qt.Key.Key_Pause: "pause",
}
_MOD_KEYS = {Qt.Key.Key_Control, Qt.Key.Key_Shift, Qt.Key.Key_Alt, Qt.Key.Key_Meta, Qt.Key.Key_AltGr,
             Qt.Key.Key_CapsLock, Qt.Key.Key_NumLock}


def hotkey_from_event(ev) -> Optional[str]:
    """Qt key event -> text understood by ``hotkey.parse_hotkey`` ("F8", "ctrl+shift+s"); None for modifier-only."""
    k = Qt.Key(ev.key()) if ev.key() in {m.value for m in Qt.Key} else None
    if k is None or k in _MOD_KEYS:
        return None
    if Qt.Key.Key_F1 <= k <= Qt.Key.Key_F24:
        name = f"F{k.value - Qt.Key.Key_F1.value + 1}"
    elif k in _QT_NAMES:
        name = _QT_NAMES[k]
    elif Qt.Key.Key_A <= k <= Qt.Key.Key_Z or Qt.Key.Key_0 <= k <= Qt.Key.Key_9:
        name = chr(k.value).lower()
    else:
        return None
    mods = ev.modifiers()
    parts = [n for m, n in ((Qt.KeyboardModifier.ControlModifier, "ctrl"), (Qt.KeyboardModifier.AltModifier, "alt"),
                            (Qt.KeyboardModifier.ShiftModifier, "shift"), (Qt.KeyboardModifier.MetaModifier, "win"))
             if mods & m]
    return "+".join(parts + [name])


class HotkeyEdit(QLineEdit):
    """Click, then press the key (combination) you want. Esc cancels. ``editingFinished`` fires when a key is taken."""

    def __init__(self) -> None:
        super().__init__()
        self.setProperty("hotkey", True)
        self.setPlaceholderText(tr("Press the key you want..."))
        self.setReadOnly(False)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._before = ""

    def focusInEvent(self, e) -> None:  # noqa: N802
        self._before = self.text()
        super().focusInEvent(e)

    def keyPressEvent(self, e) -> None:  # noqa: N802
        if e.key() == Qt.Key.Key_Escape:
            self.setText(self._before)
            self.clearFocus()
            return
        name = hotkey_from_event(e)
        if name is None:
            return  # modifier alone / unsupported key: keep waiting
        self.setText(name)
        self.editingFinished.emit()

    def inputMethodEvent(self, e) -> None:  # noqa: N802
        e.ignore()


# ============================================================================ thumbnail

class Thumbnail(QLabel):
    """Dark tile showing a template image (or a placeholder icon). Hidden while a run is active (safety).

    With a fixed ``width`` / ``height`` it is a small tile; without them it expands (the big preview in the target card).
    Pictures are shown at the largest whole-number zoom that fits (nearest neighbour), so a tiny capture stays crisp.
    """

    def __init__(self, width: int = 0, height: int = 0, radius: int = 8) -> None:
        super().__init__()
        self.setProperty("thumbbox", True)
        self._radius = radius
        if width and height:
            self.setFixedSize(width, height)
        else:
            self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
            self.setMinimumSize(96, 64)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._pix: Optional[QPixmap] = None
        self._missing = ""
        _themed.add(self)

    def show_pixmap(self, pix: Optional[QPixmap], missing: str = "") -> None:
        self._pix, self._missing = pix, missing
        self.update()

    def paintEvent(self, _e) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        if self._pix is not None:
            p.setBrush(theme.qcolor("thumb"))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawRoundedRect(r, self._radius, self._radius)
            pad = 6 if w < 150 else 16
            aw, ah = max(1, w - 2 * pad), max(1, h - 2 * pad)
            iw, ih = max(1, self._pix.width()), max(1, self._pix.height())
            dpr = self.devicePixelRatioF()
            k = min(aw / iw, ah / ih)
            if k >= 1:
                k = float(int(k))  # whole-number zoom: no blur on a tiny capture
                mode = Qt.TransformationMode.FastTransformation
            else:
                mode = Qt.TransformationMode.SmoothTransformation
            s = self._pix.scaled(max(1, int(iw * k * dpr)), max(1, int(ih * k * dpr)),
                                 Qt.AspectRatioMode.IgnoreAspectRatio, mode)
            s.setDevicePixelRatio(dpr)
            p.drawPixmap(int((w - s.width() / dpr) / 2), int((h - s.height() / dpr) / 2), s)
        else:
            p.setBrush(theme.qcolor("inp"))
            p.setPen(QPen(theme.qcolor("bd2"), 1, Qt.PenStyle.DashLine))
            p.drawRoundedRect(r, self._radius, self._radius)
            n = 28 if min(w, h) >= 56 else 20
            ic = icons.pixmap("image", theme.tok("tx3"), n, 2.0, 1.5)
            p.drawPixmap(int((w - n) / 2), int((h - n) / 2) - (8 if self._missing else 0), ic)
            if self._missing:
                p.setPen(theme.qcolor("tx3"))
                f = QFont(self.font())
                f.setPixelSize(13)
                p.setFont(f)
                p.drawText(QRectF(4, h / 2 + 8, w - 8, 20), Qt.AlignmentFlag.AlignCenter, self._missing)
        p.end()

    def refresh_theme(self) -> None:
        self.update()


class HatchedThumb(QLabel):
    """What replaces a hidden thumbnail while a run is active: diagonal hatching + a crossed-out eye."""

    def __init__(self, width: int = 0, height: int = 0) -> None:
        super().__init__()
        if width and height:
            self.setFixedSize(width, height)
        else:
            self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
            self.setMinimumSize(96, 64)

    def paintEvent(self, _e) -> None:  # noqa: N802
        p = QPainter(self)
        paint_hatch_inline(p, QRectF(self.rect()), 8, 24)
        p.end()


def paint_hatch(p: QPainter, r: QRectF, radius: float, icon_px: int = 18) -> None:
    """Hatched placeholder (a diagonal-pattern brush) with a crossed-out eye; ends the painter."""
    paint_hatch_inline(p, r, radius, icon_px)
    p.end()


def paint_hatch_inline(p: QPainter, r: QRectF, radius: float = 8, icon_px: int = 20) -> None:
    """Hatched placeholder drawn inside another paint pass (does not end the painter)."""
    from PySide6.QtGui import QBrush

    p.save()
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    rr = r.adjusted(0.5, 0.5, -0.5, -0.5)
    p.setPen(QPen(theme.qcolor("bd"), 1))
    p.setBrush(theme.qcolor("inp"))
    p.drawRoundedRect(rr, radius, radius)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(theme.qcolor("bd"), Qt.BrushStyle.BDiagPattern))
    p.drawRoundedRect(rr, radius, radius)
    p.restore()
    p.drawPixmap(int(r.center().x() - icon_px / 2), int(r.center().y() - icon_px / 2),
                 icons.pixmap("eyeoff", theme.tok("tx3"), icon_px, 2.0, 1.5))


# ============================================================================ step list (plain rows)

ROLE_ID = Qt.ItemDataRole.UserRole
ROLE_VIEW = Qt.ItemDataRole.UserRole + 1

ROW_H = 60
CARD_H = ROW_H  # (kept name) the painted row fills its whole slot


class StepDelegate(QStyledItemDelegate):
    """Paints one step as a plain row: number, 40 px thumbnail, name, optional warning line, on/off switch.

    The selected row is tinted and shows a drag grip; the step the runner is on gets a 3 px coral bar.
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.current_key: Optional[str] = None  # id of the step the runner is on
        self._sw: dict = {}  # step id -> [position 0..1, target, time of the last frame]: the row switches' slide animation
        self._tick_pending = False

    def sizeHint(self, option, index) -> QSize:  # noqa: N802
        return QSize(option.rect.width(), ROW_H)

    @staticmethod
    def card_rect(rect: QRect) -> QRect:
        return QRect(rect.x(), rect.y(), rect.width(), ROW_H)

    @staticmethod
    def switch_rect(card: QRect) -> QRect:
        return QRect(card.right() - 8 - 36, card.center().y() - 10, 36, 20)

    def _switch_pos(self, key, on: bool) -> float:
        """Where the row switch's thumb is right now (0..1). A change of ``on`` starts a slide from the current position, so
        flipping again mid-slide reverses it smoothly; the first time a row is seen it is simply drawn in its state."""
        now, target = time.monotonic(), 1.0 if on else 0.0
        st = self._sw.get(key)
        if st is None:
            self._sw[key] = [target, target, now]
            return target
        pos, tgt, last = st
        if tgt != target:
            tgt, last = target, now
        step = min(now - last, 0.05) / (SWITCH_MS / 1000)
        pos = min(pos + step, tgt) if tgt > pos else max(pos - step, tgt)
        st[:] = [pos, tgt, now]
        if pos != tgt and not self._tick_pending:  # keep repainting until the thumb has arrived
            self._tick_pending = True
            QTimer.singleShot(15, self._tick)
        return pos

    def _tick(self) -> None:
        self._tick_pending = False
        view = self.parent()
        if view is not None:
            view.viewport().update()

    def paint(self, p: QPainter, option, index) -> None:
        view = index.data(ROLE_VIEW) or {}
        card = self.card_rect(option.rect)
        enabled_row = bool(option.state & QStyle.StateFlag.State_Enabled)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hover = bool(option.state & QStyle.StateFlag.State_MouseOver)
        running_here = index.data(ROLE_ID) == self.current_key and self.current_key is not None
        on = bool(view.get("enabled", True))
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        if not enabled_row and not running_here:
            p.setOpacity(0.6)
        body = QRectF(card).adjusted(0, 2, 0, -2)
        if selected or running_here:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(theme.qcolor("tb"))
            p.drawRoundedRect(body, 8, 8)
        elif hover and enabled_row:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(theme.qcolor("hv"))
            p.drawRoundedRect(body, 8, 8)
        if running_here:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(theme.qcolor("ac"))
            p.drawRoundedRect(QRectF(card.x(), card.y() + 10, 3, ROW_H - 20), 1.5, 1.5)
        cy = card.center().y()
        if selected and enabled_row:  # grip on the selected row (a hover-only grip needs delegate hover state)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(theme.qcolor("tx3"))
            for dx in (0, 4):
                for dy in (-5, 0, 5):
                    p.drawEllipse(QPointF(card.x() + 8 + dx, cy + dy), 1.0, 1.0)
        # number
        nfont = QFont(option.font)
        nfont.setPixelSize(14)
        p.setFont(nfont)
        p.setPen(theme.qcolor("tx3"))
        p.drawText(QRect(card.x() + 16, card.y(), 22, ROW_H), Qt.AlignmentFlag.AlignVCenter,
                   str(view.get("index", index.row() + 1)))
        # thumbnail 40 x 40
        tr_ = QRectF(card.x() + 40, cy - 20, 40, 40)
        if not on:
            p.setOpacity(p.opacity() * 0.5)
        pix = view.get("thumb")
        if view.get("hidden"):
            paint_hatch_inline(p, tr_, 6, 16)
        elif pix is not None:
            p.setBrush(theme.qcolor("thumb"))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawRoundedRect(tr_, 6, 6)
            s = pix.scaled(64, 64, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
            s.setDevicePixelRatio(2.0)
            p.drawPixmap(int(tr_.center().x() - s.width() / 4), int(tr_.center().y() - s.height() / 4), s)
        else:
            p.setBrush(theme.qcolor("inp"))
            p.setPen(QPen(theme.qcolor("bd2"), 1, Qt.PenStyle.DashLine))
            p.drawRoundedRect(tr_.adjusted(0.5, 0.5, -0.5, -0.5), 6, 6)
            p.drawPixmap(int(tr_.center().x() - 9), int(tr_.center().y() - 9),
                         icons.pixmap("image", theme.tok("tx3"), 18, 2.0, 1.5))
        # name (+ warnings: only what needs the user's attention)
        font = QFont(option.font)
        font.setPixelSize(14)
        font.setWeight(theme.semibold())
        p.setFont(font)
        left = card.x() + 92
        right = self.switch_rect(card).left() - 10
        warns = [(t, tone) for t, tone in view.get("tags", []) if tone in ("er", "wn")]
        name_rect = QRect(left, card.y() + (9 if warns else 0), right - left, 22 if warns else ROW_H)
        p.setPen(theme.qcolor("tx"))
        fm = QFontMetrics(font)
        p.drawText(name_rect, Qt.AlignmentFlag.AlignVCenter,
                   fm.elidedText(str(view.get("name", "")), Qt.TextElideMode.ElideRight, max(20, right - left)))
        if warns:
            small = QFont(option.font)
            small.setPixelSize(13)
            p.setFont(small)
            sfm = QFontMetrics(small)
            x = left
            for text, tone in warns:
                glyph = icons.status_pixmap("fail" if tone == "er" else "warn", 12, 2.0)
                w = sfm.horizontalAdvance(text)
                if x + 16 + w > right:
                    break
                p.drawPixmap(x, card.y() + 33, glyph)
                p.setPen(theme.qcolor("er" if tone == "er" else "tx2"))
                p.drawText(QRect(x + 16, card.y() + 31, w + 4, 18), Qt.AlignmentFlag.AlignVCenter, text)
                x += 16 + w + 12
        p.restore()
        # switch (drawn at full opacity so it stays readable; dimmed when the list is locked)
        p.save()
        paint_switch(p, self.switch_rect(card), on, enabled_row, self._switch_pos(index.data(ROLE_ID), on))
        p.restore()

    def editorEvent(self, event, model, option, index) -> bool:  # noqa: N802
        from PySide6.QtCore import QEvent

        kind = event.type()
        if kind in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonDblClick, QEvent.Type.MouseButtonRelease):
            # The switch reacts to the press, and a quick second click arrives as a double-click event: both flip it, so
            # no click is dropped. (The base class would also treat a release near the left edge as a click on a check box.)
            if kind != QEvent.Type.MouseButtonRelease and event.button() == Qt.MouseButton.LeftButton:
                card = self.card_rect(option.rect)
                if self.switch_rect(card).adjusted(-4, -4, 4, 4).contains(event.position().toPoint()):
                    cur = index.data(Qt.ItemDataRole.CheckStateRole)
                    checked = int(getattr(cur, "value", cur) or 0) == Qt.CheckState.Checked.value  # int or enum, by binding
                    model.setData(index, Qt.CheckState.Unchecked if checked else Qt.CheckState.Checked,
                                  Qt.ItemDataRole.CheckStateRole)
                    return True
            return False
        return super().editorEvent(event, model, option, index)


class StepList(QListWidget):
    """The steps as plain rows. Drag to reorder; the switch on each row enables / disables the step."""

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("steps")
        self.delegate = StepDelegate(self)
        self.setItemDelegate(self.delegate)
        self.setDragDropMode(QListWidget.DragDropMode.InternalMove)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        self.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        self.setMouseTracking(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setSpacing(0)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.viewport().setAutoFillBackground(False)

    def set_current_step(self, step_id: Optional[str]) -> None:
        self.delegate.current_key = step_id
        self.viewport().update()
