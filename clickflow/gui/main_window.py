"""The ClickFlow main window."""
from __future__ import annotations

import datetime as dt
import json
import logging
import math
import time
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QObject, QRect, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (QAction, QColor, QGuiApplication, QIcon, QKeySequence, QPainter, QPixmap, QTextCharFormat,
                           QTextCursor)
from PySide6.QtWidgets import (
    QAbstractSpinBox, QApplication, QFileDialog, QFrame, QHBoxLayout, QLabel, QListWidgetItem, QMainWindow, QMenu,
    QPlainTextEdit, QProgressBar, QPushButton, QScrollArea, QSizePolicy, QStackedWidget, QToolButton, QVBoxLayout,
    QWidget,
)

from .. import platform_utils, storage
from .. import presets as presets_mod
from ..hotkey import HotkeyListener, parse_hotkey
from ..keystate import StopKeyWatcher
from ..inputs import NullInput, PyAutoGuiInput
from ..models import (FAST_WAIT, SLOW_INTERVAL, Preset, Profile, Step, click_interval, display_interval,
                      effective_region, effective_template, set_region)
from ..runner import Event, Hit, RunResult, Runner
from ..screen import MonitorInfo, MssScreen
from . import dialogs, i18n, icons, prefs, theme
from .i18n import tr
from .overlays import CaptureOverlay, Highlighter, StopBadge
from .qtutils import bgr_to_pixmap
from .step_editor import LABEL_COL_W, EndConditionEditor, StepEditor, row_label
from .widgets import (
    ROLE_ID, ROLE_VIEW, ROW_H, Alert, Group, HotkeyEdit, MatchMeter, Pill, SafeComboBox, SafeDoubleSpinBox, SafeSlider,
    SafeSpinBox, Segmented, StepList, Switch, SwitchRow, TabStrip, Tile, button, field, hline, icon_button, label,
    repolish, set_icon, tag,
)
from .worker import CallThread, RunThread

log = logging.getLogger("clickflow")

LEFT_COL_W = 288          # the step column (248 below ~1100 px window width)
LEFT_COL_COMPACT_W = 248


class _InfiniteProxy(QObject):
    """``s_infinite`` as a checkbox-like object backed by the "N times | Infinite" segmented control."""

    toggled = Signal(bool)

    def __init__(self, seg: Segmented) -> None:
        super().__init__(seg)
        self._seg = seg
        seg.currentIndexChanged.connect(lambda i: self.toggled.emit(i == 1))

    def isChecked(self) -> bool:  # noqa: N802
        return self._seg.currentIndex() == 1

    def setChecked(self, on: bool) -> None:  # noqa: N802
        self._seg.setCurrentIndex(1 if on else 0)

    def isEnabled(self) -> bool:  # noqa: N802
        return self._seg.isEnabled()


class MainWindow(QMainWindow):
    stop_hotkey_pressed = Signal()

    def __init__(self, profile: Optional[Profile] = None, *, start_hotkey: bool = True, defer_hotkey: bool = False) -> None:
        super().__init__()
        self._defer_hotkey = defer_hotkey and start_hotkey  # start the hotkey listeners in finish_startup(), after the first paint
        self.setWindowTitle("ClickFlow")
        self.setMinimumSize(960, 640)
        self._fit_to_screen()
        self.profile = profile or Profile(name="Untitled")
        self.dirty = False
        self._run_thread: Optional[RunThread] = None
        self._call_thread: Optional[CallThread] = None
        self._overlay: Optional[CaptureOverlay] = None
        self._was_minimized_for_run = False
        self.highlighter = Highlighter()
        self.stop_badge = StopBadge()
        self.stop_badge.clicked.connect(self.stop_run)
        self._building = False
        self._monitors: list[MonitorInfo] = []
        self._advanced = prefs.get("mode") == "advanced"
        self._running = False
        self._thumb_cache: dict[str, tuple[float, QPixmap]] = {}
        self._stats = self._new_stats()
        self._clock = QTimer(self)
        self._clock.setInterval(250)
        self._clock.timeout.connect(self._tick_dashboard)
        self._last_max_clicks = 500
        self._log_entries: list[tuple[str, str, str, str]] = []
        self._capture_kind = "target"  # what the next overlay selects: "target" (cream) or "area" (teal)
        self._pending_new_step: Optional[str] = None  # id of a step created by 'Add step' whose first capture is still open

        self._build_ui()
        self._build_actions()
        self.stop_hotkey_pressed.connect(self.stop_run)
        self.hotkey = HotkeyListener(self.profile.settings.stop_hotkey or "F8", self.stop_hotkey_pressed.emit)
        self.key_watcher = StopKeyWatcher(self.profile.settings.stop_hotkey or "F8", self.stop_hotkey_pressed.emit)
        self._hotkey_enabled = start_hotkey
        self.load_into_ui(self.profile)
        self._set_running(False)

    def finish_startup(self) -> None:
        """Work that is not needed to paint the first screen (pynput import, hotkey listeners); called once the window is up."""
        if self._defer_hotkey:
            self._defer_hotkey = False
            if self._hotkey_enabled:
                self._start_hotkey()

    # ================================================================== window size

    @staticmethod
    def initial_size(available_width: int, available_height: int) -> tuple[int, int]:
        """Preferred window size that always fits the screen it opens on (laptops of 1366x768 and smaller)."""
        return (min(1180, int(available_width * 0.95)), min(820, int(available_height * 0.92)))

    def _fit_to_screen(self) -> None:
        screen = QGuiApplication.primaryScreen()
        if screen is None:
            self.resize(1180, 820)
            return
        avail = screen.availableGeometry()
        w, h = self.initial_size(avail.width(), avail.height())
        self.resize(w, h)
        self.move(avail.x() + max(0, (avail.width() - w) // 2), avail.y() + max(0, (avail.height() - h) // 3))

    # ================================================================== UI construction

    def _build_ui(self) -> None:
        central = QWidget()
        central.setObjectName("root")
        outer = QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self._build_topbar())

        self.main_stack = QStackedWidget()
        self.main_stack.addWidget(self._build_workspace())   # 0
        self.main_stack.addWidget(self._build_empty_state())  # 1
        outer.addWidget(self.main_stack, 1)
        outer.addWidget(self._build_log_drawer())
        outer.addWidget(self._build_dock())
        self.setCentralWidget(central)

        self.step_editor.set_advanced(self._advanced)
        self.mode_seg.setCurrentIndex(1 if self._advanced else 0)
        self._place_run_card()
        self._on_tab_changed(self.tabs.currentIndex())
        self._compact = None
        self._apply_density()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._apply_density()

    def _apply_density(self) -> None:
        """Below ~1100 px wide the step column narrows to 248, the gap to 24 and the margins to 16 (960 x 640 must fit)."""
        compact = self.width() < 1100
        if getattr(self, "_compact", None) == compact or not hasattr(self, "_ws_lay"):
            return
        self._compact = compact
        self.steps_col.setFixedWidth(LEFT_COL_COMPACT_W if compact else LEFT_COL_W)
        self._ws_lay.setSpacing(24 if compact else 32)
        self._ws_lay.setContentsMargins(*((16, 16, 16, 12) if compact else (24, 24, 24, 16)))

    # ---------------------------------------------------------------- top bar

    @staticmethod
    def _dot_icon() -> QIcon:
        """The small coral dot that marks unsaved changes next to the profile name."""
        px = QPixmap(16, 16)
        px.fill(Qt.GlobalColor.transparent)
        p = QPainter(px)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(theme.qcolor("ac"))
        p.drawEllipse(4, 4, 8, 8)
        p.end()
        return QIcon(px)

    def _build_topbar(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("topbar")
        bar.setFixedHeight(56)
        row = QHBoxLayout(bar)
        row.setContentsMargins(24, 0, 16, 0)
        row.setSpacing(8)
        logo = QLabel()
        logo.setFixedSize(28, 28)
        try:
            from ..app import resource_root

            pm = QPixmap(str(resource_root() / "assets" / "icon.png"))
            if not pm.isNull():
                logo.setPixmap(pm.scaled(56, 56, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
                logo.setScaledContents(True)
        except Exception:
            pass
        row.addWidget(logo)
        row.addSpacing(4)

        # the profile name is the menu; a coral dot appears on it while there are unsaved changes
        self.profile_btn = QToolButton()
        self.profile_btn.setProperty("profile", True)
        self.profile_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.profile_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.profile_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.profile_btn.setMaximumWidth(360)
        self.profile_menu = QMenu(self.profile_btn)
        self.profile_menu.aboutToShow.connect(self._fill_profile_menu)
        self.profile_btn.setMenu(self.profile_menu)
        row.addWidget(self.profile_btn)

        # display preset: used (and switched) often, so it stays one click away right next to the profile name
        self.preset_btn = QToolButton()
        self.preset_btn.setProperty("topbtn", True)
        self.preset_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.preset_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.preset_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.preset_btn.setMaximumWidth(220)
        set_icon(self.preset_btn, "display", "tx2", 16)
        self.preset_menu = QMenu(tr("Display preset"), self.preset_btn)
        self.preset_menu.aboutToShow.connect(self._fill_preset_menu)
        self.preset_btn.setMenu(self.preset_menu)
        row.addWidget(self.preset_btn)
        row.addStretch(1)

        self.mode_seg = Segmented(fill=False)
        self.mode_seg.addItem(tr("Simple"), False)
        self.mode_seg.addItem(tr("Advanced"), True)
        self.mode_seg.currentIndexChanged.connect(lambda i: self.set_mode(bool(i)))
        row.addWidget(self.mode_seg, 0, Qt.AlignmentFlag.AlignVCenter)
        row.addSpacing(8)
        self.btn_lang = QToolButton()
        self.btn_lang.setProperty("topbtn", True)
        self.btn_lang.setText("EN" if i18n.language() == "zh" else "中文")
        self.btn_lang.setToolTip(tr("Switch language"))
        self.btn_lang.setCursor(Qt.CursorShape.PointingHandCursor)
        # (run after the click has finished: the rebuild deletes this very button)
        self.btn_lang.clicked.connect(lambda: QTimer.singleShot(0, self._toggle_language))
        row.addWidget(self.btn_lang)

        # everything else folds into one menu: step actions, display presets, notes, about
        self.btn_more = icon_button("more", tr("More"), "ghost", on_click=None, token="tx2")
        self.btn_more.setProperty("more", True)
        self.btn_more.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.more_menu = QMenu(self.btn_more)
        self.more_menu.aboutToShow.connect(self._fill_more_menu)
        self.btn_more.setMenu(self.more_menu)
        row.addWidget(self.btn_more)
        self._lockable_top = (self.profile_btn, self.preset_btn, self.btn_more, self.mode_seg)
        self.banner = QLabel()  # first part of the profile notes (not shown: the "Profile notes" menu entry opens them)
        self.banner.setVisible(False)
        return bar

    # ---------------------------------------------------------------- workspace (steps | editor / dashboard)

    def _build_workspace(self) -> QWidget:
        page = QWidget()
        self._ws_lay = QHBoxLayout(page)
        self._ws_lay.setContentsMargins(24, 24, 24, 16)
        self._ws_lay.setSpacing(32)
        self.steps_col = self._build_steps_col()
        self._ws_lay.addWidget(self.steps_col)
        right = QVBoxLayout()
        right.setSpacing(12)
        self.tabs = TabStrip()
        for text in (tr("Step"), tr("Match & area"), tr("Waits & fallbacks"), tr("Global")):
            self.tabs.add(text)
        self.tabs.finish()
        self.tabs.currentChanged.connect(self._on_tab_changed)
        right.addWidget(self.tabs)
        self.right_stack = QStackedWidget()
        self.right_stack.addWidget(self._scroll(self._build_step_page(), "scroll_step"))     # 0
        self.right_stack.addWidget(self._scroll(self._build_global_page(), "scroll_global"))  # 1
        self.right_stack.addWidget(self._scroll(self._build_dashboard(), "scroll_dash"))     # 2
        right.addWidget(self.right_stack, 1)
        self._ws_lay.addLayout(right, 1)
        return page

    def _scroll(self, widget: QWidget, attr: str) -> QScrollArea:
        sa = QScrollArea()
        sa.setWidgetResizable(True)
        sa.setFrameShape(QFrame.Shape.NoFrame)
        sa.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        sa.setWidget(widget)
        setattr(self, attr, sa)
        return sa

    def _build_steps_col(self) -> QWidget:
        col = QWidget()
        col.setFixedWidth(LEFT_COL_W)
        lay = QVBoxLayout(col)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(12)
        head = QWidget()
        head.setFixedHeight(32)
        hl = QHBoxLayout(head)
        hl.setContentsMargins(0, 0, 0, 0)
        hl.setSpacing(8)
        hl.addWidget(label(tr("Steps"), "section"))
        self.step_count = label("0", "count")
        hl.addWidget(self.step_count)
        hl.addStretch(1)
        lay.addWidget(head)
        self.step_list = StepList()
        self.step_list.currentRowChanged.connect(self._on_row_changed)
        self.step_list.itemChanged.connect(self._on_item_changed)
        self.step_list.model().rowsMoved.connect(self._on_rows_moved)
        lay.addWidget(self.step_list, 1)
        self.btn_add = QPushButton(tr("Add step"))
        self.btn_add.setProperty("add", True)
        self.btn_add.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_add.setToolTip(tr("Add a step and select its target on the screen right away."))
        set_icon(self.btn_add, "add", "tx2", 16)
        self.btn_add.clicked.connect(self.add_step_and_select)
        lay.addWidget(self.btn_add)
        return col

    # ---------------------------------------------------------------- step page (simple) and global page (advanced)

    def _build_step_page(self) -> QWidget:
        page = QWidget()
        self.step_page_lay = QVBoxLayout(page)
        self.step_page_lay.setContentsMargins(0, 0, 0, 0)
        self.step_page_lay.setSpacing(24)
        self.step_editor = StepEditor()
        self.step_editor.changed.connect(self._on_step_edited)
        self.step_editor.capture_requested.connect(self.capture_step_template)
        self.step_editor.region_requested.connect(self.pick_step_region)
        self.step_editor.show_boxes_requested.connect(self.show_search_boxes)
        self.step_editor.test_requested.connect(self.test_step)
        self.step_editor.guard_capture_requested.connect(self.capture_step_guard)
        self.chk_manual = self.step_editor.chk_manual
        self.chk_manual.toggled.connect(self._apply_settings)
        self.step_page_lay.addWidget(self.step_editor, 1)
        self.run_card = self._build_run_card()
        self.step_page_lay.addWidget(self.run_card)
        return page

    def _build_run_card(self) -> QWidget:
        """Speed, Repeat and Click limit as three compact rows with the 88 px label column."""
        card = QWidget()
        lay = QVBoxLayout(card)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(12)

        # speed: segmented presets, the slider under them, one line of explanation
        self.speed_seg = Segmented(fill=False)
        for v, name in ((0, tr("Slow")), (50, tr("Medium")), (100, tr("Fast"))):
            self.speed_seg.addItem(f"{name} · {display_interval(v):.2f} s", v)
        self.speed_seg.currentIndexChanged.connect(self._on_speed_preset)
        self.lbl_slow = label(tr("Slow ({s:g} s)", s=SLOW_INTERVAL), "hint")  # (not shown: the segments carry the numbers)
        self.lbl_fast = label(tr("Fast"), "hint")
        self.speed_slider = SafeSlider(Qt.Orientation.Horizontal)
        self.speed_slider.setRange(0, 100)
        self.speed_slider.setToolTip(tr("How long from one click to the next. Right = fastest (no added delay), "
                                        "left = slowest ({s:g} s between clicks).", s=SLOW_INTERVAL))
        self.lbl_speed = label("", "hint", True)
        speed = QVBoxLayout()
        speed.setSpacing(6)
        r1 = QHBoxLayout()
        r1.setSpacing(12)
        r1.addWidget(row_label(tr("Speed")), 0, Qt.AlignmentFlag.AlignVCenter)
        r1.addWidget(self.speed_seg, 0, Qt.AlignmentFlag.AlignVCenter)
        r1.addStretch(1)
        speed.addLayout(r1)
        r2 = QHBoxLayout()
        r2.setSpacing(0)
        r2.addSpacing(LABEL_COL_W + 12)
        r2.addWidget(self.speed_slider, 1)
        speed.addLayout(r2)
        r3 = QHBoxLayout()
        r3.setSpacing(0)
        r3.addSpacing(LABEL_COL_W + 12)
        r3.addWidget(self.lbl_speed, 1)
        speed.addLayout(r3)
        lay.addLayout(speed)

        # repeat
        self.repeat_seg = Segmented(fill=False)
        self.repeat_seg.addItem(tr("N times"), False)
        self.repeat_seg.addItem(tr("Infinite"), True)
        self.s_infinite = _InfiniteProxy(self.repeat_seg)
        self.s_loops = SafeSpinBox()
        self.s_loops.setRange(1, 1_000_000)
        self.s_loops.setSuffix(" " + tr("times"))
        self.s_loops.setKeyboardTracking(True)
        self.s_loops.setMinimumWidth(130)
        self.lbl_repeat = label("", "hint")
        rep = QHBoxLayout()
        rep.setSpacing(12)
        rep.addWidget(row_label(tr("Repeat")), 0, Qt.AlignmentFlag.AlignVCenter)
        rep.addWidget(self.repeat_seg, 0, Qt.AlignmentFlag.AlignVCenter)
        rep.addWidget(self.s_loops, 0, Qt.AlignmentFlag.AlignVCenter)
        rep.addWidget(self.lbl_repeat, 1, Qt.AlignmentFlag.AlignVCenter)
        lay.addLayout(rep)

        # click limit
        self.max_row = SwitchRow(tr("Stops automatically when reached"))
        self.max_row.title.setProperty("role", "muted")
        self.s_maxclicks = SafeSpinBox()
        self.s_maxclicks.setRange(0, 1_000_000)
        self.s_maxclicks.setSpecialValueText(tr("No limit"))
        self.s_maxclicks.setSuffix(" " + tr("clicks"))
        self.s_maxclicks.setKeyboardTracking(True)
        self.s_maxclicks.setMinimumWidth(130)
        lim = QHBoxLayout()
        lim.setSpacing(12)
        lim.addWidget(row_label(tr("Click limit")), 0, Qt.AlignmentFlag.AlignVCenter)
        lim.addWidget(self.s_maxclicks, 0, Qt.AlignmentFlag.AlignVCenter)
        lim.addWidget(self.max_row, 1, Qt.AlignmentFlag.AlignVCenter)
        lay.addLayout(lim)
        self.max_row.switch.toggled.connect(self._on_max_switch)

        self.speed_slider.valueChanged.connect(self._apply_settings)
        for w in (self.s_loops, self.s_maxclicks):
            w.valueChanged.connect(self._apply_settings)
        self.s_infinite.toggled.connect(self._apply_settings)
        return card

    def _build_global_page(self) -> QWidget:
        """Advanced > Global: two columns. The run rows (speed / repeat / click limit) are moved in at the top of the left one."""
        page = QWidget()
        self.global_lay = QVBoxLayout(page)  # the run card is inserted at index 0 in advanced mode, above the columns
        self.global_lay.setContentsMargins(0, 0, 0, 0)
        self.global_lay.setSpacing(24)
        cols = QHBoxLayout()
        cols.setSpacing(32)
        self.global_left = QVBoxLayout()
        self.global_left.setSpacing(24)
        self.global_right = QVBoxLayout()
        self.global_right.setSpacing(24)
        cols.addLayout(self.global_left, 1)
        cols.addLayout(self.global_right, 1)
        self.global_lay.addLayout(cols)
        self.global_lay.addStretch(1)
        self.global_slot_index = 0

        # stop & limits ---------------------------------------------------------
        g = Group(tr("Stop & limits"), flat=True)
        self.s_idle = SafeSpinBox()
        self.s_idle.setRange(0, 100000)
        self.s_idle.setSpecialValueText(tr("Never"))
        self.s_idle.setToolTip(tr("Stop after this many loops in a row in which nothing was found/clicked."))
        self.s_idle.setKeyboardTracking(True)
        g.add(field(tr("Stop after N empty loops"), self.s_idle))
        self.s_hotkey = HotkeyEdit()
        self.s_hotkey.setToolTip(tr("Global stop hotkey. Click the field, then press the key you want."))
        self.btn_hotkey_reset = button(tr("Reset to F8"), size="")
        self.btn_hotkey_reset.clicked.connect(lambda: (self.s_hotkey.setText("F8"), self._apply_hotkey()))
        hk = QHBoxLayout()
        hk.setSpacing(8)
        hk.addWidget(self.s_hotkey, 1)
        hk.addWidget(self.btn_hotkey_reset)
        hk_wrap = QWidget()
        hk_wrap.setLayout(hk)
        g.add(field(tr("Stop hotkey"), hk_wrap))
        self.corner_row = SwitchRow(tr("Stop when mouse hits a corner"))
        self.s_corner = self.corner_row.switch
        g.add(self.corner_row)
        self.grp_limits = g
        self.global_left.addWidget(g)

        # timing & display -------------------------------------------------------
        g = Group(tr("Timing & display"), flat=True)
        self.s_jitter = SafeDoubleSpinBox()
        self.s_jitter.setRange(0, 100)
        self.s_jitter.setSuffix(" %")
        self.s_jitter.setToolTip(tr("Every wait is randomised by +/- this percentage."))
        self.s_start_delay = SafeDoubleSpinBox()
        self.s_start_delay.setRange(0, 600)
        self.s_start_delay.setSuffix(" s")
        self.s_scan = SafeDoubleSpinBox()
        self.s_scan.setRange(0.02, 10)
        self.s_scan.setSingleStep(0.05)
        self.s_scan.setSuffix(" s")
        self.s_monitor = SafeComboBox()
        self.s_monitor.setToolTip(tr("Which screen to scan. \"All screens\" combines every monitor."))
        for w in (self.s_jitter, self.s_start_delay, self.s_scan):
            w.setKeyboardTracking(True)
        row1 = QHBoxLayout()
        row1.setSpacing(12)
        row1.addWidget(field(tr("Randomize timing"), self.s_jitter), 1)
        row1.addWidget(field(tr("Start delay"), self.s_start_delay), 1)
        row2 = QHBoxLayout()
        row2.setSpacing(12)
        row2.addWidget(field(tr("Scan interval"), self.s_scan), 1)
        row2.addWidget(field(tr("Display"), self.s_monitor), 1)
        g.add(row1)
        g.add(row2)
        self.minimize_row = SwitchRow(tr("Minimize while running"))
        self.s_minimize = self.minimize_row.switch
        g.add(self.minimize_row)
        self.grp_timing = g
        self.global_right.addWidget(g)

        # end condition -------------------------------------------------------------
        self.end_editor = EndConditionEditor()
        self.end_editor.changed.connect(self._mark_dirty)
        self.end_editor.capture_requested.connect(self.capture_end_template)
        self.end_editor.region_requested.connect(self.pick_end_region)
        self.global_right.addWidget(self.end_editor)

        # display presets -------------------------------------------------------------
        g = Group(tr("Display presets"), flat=True)
        g.add(label(tr("Save different search areas per screen resolution."), "hint", True))
        self.preset_combo = SafeComboBox()
        self.preset_combo.setToolTip(tr("A preset stores the search areas (and optionally target images) for one display "
                                        "resolution. Edits you make while a preset is selected go into that preset."))
        self.preset_combo.currentIndexChanged.connect(self._on_preset_selected)
        self.btn_preset_new = button(tr("New"), on_click=self.new_preset)
        self.btn_preset_rename = button(tr("Rename"), on_click=self.rename_preset)
        self.btn_preset_delete = button(tr("Delete"), on_click=self.delete_preset)
        g.add(self.preset_combo)
        prow = QHBoxLayout()
        prow.setSpacing(8)
        for b in (self.btn_preset_new, self.btn_preset_rename, self.btn_preset_delete):
            prow.addWidget(b)
        prow.addStretch(1)
        g.add(prow)
        self.auto_row = SwitchRow(tr("Auto-switch by detected resolution"))
        self.auto_row.setToolTip(tr("At the start of a run, ClickFlow detects the connected monitors and picks the preset "
                                    "whose resolution matches (and the monitor it applies to)."))
        self.chk_auto_preset = self.auto_row.switch
        self.chk_auto_preset.toggled.connect(self._apply_settings)
        g.add(self.auto_row)
        self.lbl_displays = label("", "hint", True)
        self.btn_detect = button(tr("Detect again"), on_click=self._refresh_displays_label)
        g.add(self.lbl_displays)
        g.add(self.btn_detect)
        self.preset_bar = g
        self.global_left.addWidget(g)

        # notes -------------------------------------------------------------------------
        g = Group(tr("Profile notes"), flat=True)
        self.notes = QPlainTextEdit()
        self.notes.setPlaceholderText(tr("Notes about this profile..."))
        self.notes.setMinimumHeight(90)
        self.notes.textChanged.connect(self._on_notes_changed)
        g.add(self.notes)
        self.global_right.addWidget(g)
        self.global_left.addStretch(1)
        self.global_right.addStretch(1)

        for w in (self.s_idle, self.s_jitter, self.s_start_delay, self.s_scan):
            w.valueChanged.connect(self._apply_settings)
        self.s_monitor.currentIndexChanged.connect(self._apply_settings)
        for w in (self.s_corner, self.s_minimize):
            w.toggled.connect(self._apply_settings)
        self.s_hotkey.editingFinished.connect(self._apply_hotkey)
        return page

    # ---------------------------------------------------------------- dashboard (shown while running)

    def _build_dashboard(self) -> QWidget:
        """Running: the status word first, then progress, four figures, one match line and one notice. No boxes."""
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(24)

        top = QHBoxLayout()
        top.setSpacing(16)
        self.dash_pill = Pill("run", tr("Running"), big=True)
        top.addWidget(self.dash_pill)
        top.addStretch(1)
        self.dash_stop_hint = label("", "hint")
        top.addWidget(self.dash_stop_hint, 0, Qt.AlignmentFlag.AlignBottom)
        lay.addLayout(top)
        self.dash_text = label("", "muted")
        lay.addWidget(self.dash_text)

        prog = QVBoxLayout()
        prog.setSpacing(6)
        self.dash_progress = QProgressBar()
        self.dash_progress.setRange(0, 1000)
        self.dash_progress.setTextVisible(False)
        prog.addWidget(self.dash_progress)
        prow = QHBoxLayout()
        self.dash_prog_l = label("", "hint")
        self.dash_prog_r = label("", "hint")
        prow.addWidget(self.dash_prog_l)
        prow.addStretch(1)
        prow.addWidget(self.dash_prog_r)
        prog.addLayout(prow)
        lay.addLayout(prog)

        tiles = QHBoxLayout()
        tiles.setSpacing(24)
        self.tile_clicks = Tile(tr("Clicks"))
        self.tile_loops = Tile(tr("Loops"))
        self.tile_time = Tile(tr("Elapsed"))
        self.tile_step = Tile(tr("Current step"))
        for t in (self.tile_clicks, self.tile_loops, self.tile_time, self.tile_step):
            tiles.addWidget(t, 1)
        lay.addLayout(tiles)

        # one match line + a thin confidence bar
        ml = QVBoxLayout()
        ml.setSpacing(6)
        mh = QHBoxLayout()
        mh.setSpacing(10)
        mh.addWidget(label(tr("Latest match"), "field"))
        self.match_tag = tag("", "ok")
        self.match_tag.setVisible(False)
        mh.addWidget(self.match_tag)
        self.match_name = label(tr("No match yet"), "section")
        mh.addWidget(self.match_name)
        mh.addStretch(1)
        self.match_pos = label("", "mono")
        mh.addWidget(self.match_pos)
        self.match_val = label("", "mono")
        mh.addWidget(self.match_val)
        ml.addLayout(mh)
        self.match_meter = MatchMeter()
        ml.addWidget(self.match_meter)
        sr = QHBoxLayout()
        sr.addWidget(label("0.50", "hint"))
        sr.addStretch(1)
        self.match_thr_lbl = label("", "hint")
        sr.addWidget(self.match_thr_lbl)
        sr.addStretch(1)
        sr.addWidget(label("1.00", "hint"))
        ml.addLayout(sr)
        lay.addLayout(ml)
        lay.addWidget(Alert(tr("Target thumbnails are hidden while running so the matcher cannot find them on screen."), "info"))
        lay.addStretch(1)
        return page

    # ---------------------------------------------------------------- empty state

    def _build_empty_state(self) -> QWidget:
        """Heading, one sentence, one primary button, three numbered columns."""
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(24, 24, 24, 24)
        outer.addStretch(1)
        col = QVBoxLayout()
        col.setSpacing(16)
        col.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        h = label(tr("Let it click for you"), "h1")
        h.setAlignment(Qt.AlignmentFlag.AlignCenter)
        col.addWidget(h)
        sub = label(tr("Select a small image on screen (the target). ClickFlow keeps looking for it and clicks it."), "muted", True)
        sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        sub.setFixedWidth(560)
        sub.setFixedHeight(sub.heightForWidth(560) + 4)
        col.addWidget(sub, 0, Qt.AlignmentFlag.AlignHCenter)
        btns = QHBoxLayout()
        btns.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self.btn_empty_add = button(tr("Add step"), "", "primary", "lg", on_click=self.add_step_and_select)
        btns.addWidget(self.btn_empty_add)
        col.addLayout(btns)
        col.addSpacing(16)
        steps = QHBoxLayout()
        steps.setSpacing(40)
        steps.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        data = ((tr("Select a target"), tr("Drag a box around the button or icon you want clicked.")),
                (tr("Pick speed and repeats"), tr("Three speeds; click N times or loop forever.")),
                (tr("Press Start"), tr("Stop any time with the hotkey. No risk of clicking too long.")))
        for i, (title, desc) in enumerate(data, 1):
            c = QWidget()
            c.setFixedWidth(230)
            cl = QVBoxLayout(c)
            cl.setContentsMargins(0, 0, 0, 0)
            cl.setSpacing(4)
            cl.addWidget(label(str(i), "hint"))
            cl.addWidget(label(title, "section"))
            cl.addWidget(label(desc, "muted", True))
            cl.addStretch(1)
            steps.addWidget(c)
        col.addLayout(steps)
        ex = QHBoxLayout()
        ex.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self.btn_empty_example = button(tr("Open the example profile"), "", "link", on_click=self.open_example)
        ex.addWidget(self.btn_empty_example)
        col.addSpacing(8)
        col.addLayout(ex)
        outer.addLayout(col)
        outer.addStretch(2)
        return page

    # ---------------------------------------------------------------- log panel (dark code surface) and bottom bar

    def _build_log_drawer(self) -> QWidget:
        box = QFrame()
        box.setObjectName("logdrawer")
        self.log_drawer = box
        lay = QVBoxLayout(box)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        head = QHBoxLayout()
        head.setContentsMargins(24, 10, 16, 0)
        head.setSpacing(8)
        dim = f"color: {theme.tok('lg_t')};"
        title = label(tr("Live log"))
        title.setStyleSheet(dim + " font-size: 13px;")
        head.addWidget(title)
        self.log_count = label("0")
        self.log_count.setStyleSheet(dim + " font-size: 13px;")
        head.addWidget(self.log_count)
        self.log_last = label("")  # (kept for callers; the panel itself shows the lines)
        self.log_last.setVisible(False)
        head.addStretch(1)
        self.btn_log_clear = button(tr("Clear"), variant="ghost", size="sm", on_click=self._clear_log)
        self.btn_log_clear.setStyleSheet(
            f"QPushButton {{ color: {theme.tok('lg_t')}; }}"
            f"QPushButton:hover {{ color: {theme.tok('lg_i')}; background: rgba(250,249,245,0.12); }}")
        head.addWidget(self.btn_log_clear)
        lay.addLayout(head)
        self.log_view = QPlainTextEdit()
        self.log_view.setProperty("role", "log")
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(3000)
        self.log_view.setFixedHeight(150)
        lay.addWidget(self.log_view)
        self._log_n = 0
        box.setVisible(False)
        return box

    def _set_log_open(self, on: bool) -> None:
        self.log_drawer.setVisible(on)
        if hasattr(self, "log_toggle"):
            self.log_toggle.setChecked(on)

    def _clear_log(self) -> None:
        self._log_entries.clear()
        self.log_view.clear()
        self._log_n = 0
        self.log_count.setText("0")
        self.log_last.setText("")

    def _build_dock(self) -> QWidget:
        dock = QFrame()
        dock.setObjectName("dock")
        dock.setFixedHeight(72)
        row = QHBoxLayout(dock)
        row.setContentsMargins(24, 0, 24, 0)
        row.setSpacing(12)
        self.pill = Pill("idle", tr("Idle"))
        row.addWidget(self.pill)
        self.dock_hint = label("", "hint")
        self.dock_hint.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        row.addWidget(self.dock_hint, 1)
        self.log_toggle = QToolButton()
        self.log_toggle.setProperty("logtoggle", True)
        self.log_toggle.setText(tr("Log"))
        self.log_toggle.setCheckable(True)
        self.log_toggle.setToolTip(tr("Live log"))
        self.log_toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        self.log_toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        set_icon(self.log_toggle, "log", "tx2", 16)
        self.log_toggle.toggled.connect(lambda on: self.log_drawer.setVisible(on))
        row.addWidget(self.log_toggle)
        self.btn_dry = button(tr("Dry run (no clicks)"), size="lg", on_click=lambda: self.start_run(dry=True))
        self.btn_dry.setToolTip(tr("Looks for the targets and draws boxes, but never clicks.") + "  Ctrl+D")
        self.btn_start = button(tr("Start"), "play", "primary", "lg", on_click=self._start_or_stop)
        self.btn_start.setMinimumWidth(130)
        self.btn_start.setToolTip("Ctrl+R")
        row.addWidget(self.btn_dry)
        row.addWidget(self.btn_start)
        return dock

    # ================================================================== actions (shortcuts and profile menu)

    def _build_actions(self) -> None:
        for a in list(self.actions()):
            self.removeAction(a)

        def act(text, slot, shortcut=None):
            a = QAction(text, self)
            a.triggered.connect(slot)
            if shortcut:
                a.setShortcut(QKeySequence(shortcut))
                a.setShortcutContext(Qt.ShortcutContext.WindowShortcut)
            self.addAction(a)
            return a

        self.act_new = act(tr("New profile..."), self.new_profile, "Ctrl+N")
        self.act_open = act(tr("Open..."), self.open_profile, "Ctrl+O")
        self.act_example = act(tr("Open example profile"), self.open_example)
        self.act_save = act(tr("Save"), self.save, "Ctrl+S")
        self.act_saveas = act(tr("Save as..."), self.save_as, "Ctrl+Shift+S")
        self.act_import = act(tr("Import .zip..."), self.import_zip)
        self.act_export = act(tr("Export .zip..."), self.export_zip)
        self.act_quit = act(tr("Quit"), self.close, "Ctrl+Q")
        self.act_run = act(tr("Start"), lambda: self.start_run(dry=False), "Ctrl+R")
        self.act_dry = act(tr("Dry run (no clicks)"), lambda: self.start_run(dry=True), "Ctrl+D")
        self.act_stop = act(tr("Stop"), self.stop_run, "Ctrl+.")
        self.act_dup = act(tr("Duplicate step"), self.duplicate_step)
        self.act_up = act(tr("Move step up"), lambda: self.move_step(-1))
        self.act_down = act(tr("Move step down"), lambda: self.move_step(1))
        self.act_del = act(tr("Delete step"), self.delete_step)

    def _recent_profiles(self, limit: int = 8) -> list[tuple[str, Path]]:
        out: list[tuple[float, str, Path]] = []
        try:
            for d in storage.profiles_root().iterdir():
                f = d / "profile.json"
                if f.is_file():
                    try:
                        name = json.loads(f.read_text(encoding="utf-8")).get("name") or d.name
                    except (OSError, ValueError):
                        name = d.name
                    out.append((f.stat().st_mtime, str(name), d))
        except OSError:
            pass
        out.sort(key=lambda t: t[0], reverse=True)
        return [(n, d) for _m, n, d in out[:limit]]

    def _fill_profile_menu(self) -> None:
        m = self.profile_menu
        m.clear()
        m.addSection(tr("Recent"))
        cur = Path(self.profile.directory).resolve() if self.profile.directory else None
        for name, d in self._recent_profiles():
            a = m.addAction(name)
            a.setCheckable(True)
            try:
                a.setChecked(cur is not None and d.resolve() == cur)
            except OSError:
                pass
            a.triggered.connect(lambda _=False, p=d: self._open_recent(p))
        m.addSeparator()
        for a, ic in ((self.act_new, "add"), (self.act_open, "folder"), (self.act_example, "file")):
            a.setIcon(icons.icon(ic, theme.tok("tx2"), 16))
            m.addAction(a)
        m.addSeparator()
        for a, ic in ((self.act_save, "save"), (self.act_saveas, "save"), (self.act_import, "export"), (self.act_export, "export")):
            a.setIcon(icons.icon(ic, theme.tok("tx2"), 16))
            m.addAction(a)

    def _fill_more_menu(self) -> None:
        """The "more" menu: step actions, display presets, profile notes, about."""
        m = self.more_menu
        m.clear()
        row, n = self.step_list.currentRow(), self.step_list.count()
        have = row >= 0 and not self._running
        for act, ok in ((self.act_dup, have), (self.act_up, have and row > 0), (self.act_down, have and row < n - 1),
                        (self.act_del, have)):
            act.setEnabled(ok)
            m.addAction(act)
        m.addSeparator()
        if self.profile.notes.strip():
            m.addAction(tr("Profile notes"), self._show_notes)
        m.addSeparator()
        m.addAction(tr("About & safety").replace("&", "&&"), self.show_about)
        m.addAction(tr("Open-source licenses"), self.show_licenses)

    def _open_recent(self, path: Path) -> None:
        if self._running or not self._confirm_discard():
            return
        self.open_path(path)

    # ================================================================== mode / theme / language

    def set_mode(self, advanced: bool) -> None:
        self._advanced = advanced
        prefs.set("mode", "advanced" if advanced else "simple")
        if self.mode_seg.currentIndex() != (1 if advanced else 0):
            self.mode_seg.blockSignals(True)
            self.mode_seg.setCurrentIndex(1 if advanced else 0)
            self.mode_seg.blockSignals(False)
        self.step_editor.set_advanced(advanced)
        self._place_run_card()
        self._on_tab_changed(self.tabs.currentIndex())

    def _place_run_card(self) -> None:
        """The run settings card sits under the step in simple mode and at the top of Global settings in advanced mode."""
        for lay in (self.step_page_lay, self.global_lay):
            lay.removeWidget(self.run_card)
        if self._advanced:
            self.global_lay.insertWidget(0, self.run_card)
        else:
            self.step_page_lay.insertWidget(1, self.run_card)
        self.run_card.show()
        self.chk_manual.setVisible(True)

    def _open_global_tab(self) -> None:
        if not self._advanced:
            self.set_mode(True)
        self.tabs.setCurrentIndex(3)

    def _on_tab_changed(self, i: int) -> None:
        if self._running:
            return
        self.tabs.setVisible(self._advanced)
        # simple mode has a single page; advanced mode: tabs 0-2 are sections of the step editor, tab 3 is Global
        glob = self._advanced and i == 3
        self.right_stack.setCurrentIndex(1 if glob else 0)
        self.step_editor.set_section(i if self._advanced and i < 3 else 0)

    def _toggle_language(self) -> None:
        if self._running:
            return
        i18n.set_language("en" if i18n.language() == "zh" else "zh")
        self._rebuild_ui()  # (also restyles: the stylesheet depends on the language)

    def _profile_fingerprint(self) -> str:
        try:
            return json.dumps(self.profile.to_dict(), sort_keys=True, default=str)
        except (TypeError, ValueError):
            return str(id(object()))  # (cannot compare: treat as changed)

    def _rebuild_ui(self) -> None:
        """Rebuild every caption in the other language: the widget tree is cheap to recreate and keeps no stale text.

        Done in the order that is quickest and that never shows a half-built window: painting is frozen, the old tree is
        destroyed at once (nothing left to restyle), the new tree is built without a stylesheet, and the stylesheet is
        applied once to the finished tree. The user sees the old window, then the new one."""
        import shiboken6

        was_dirty, before = self.dirty, self._profile_fingerprint()
        self._flush_pending_edits()
        # committing the editors always reports "changed"; it only counts if a typed value really changed the profile
        dirty = self.dirty if self._profile_fingerprint() != before else was_dirty
        row, tab, entries = self.step_list.currentRow(), self.tabs.currentIndex(), list(self._log_entries)
        self.setUpdatesEnabled(False)
        try:
            old = self.takeCentralWidget()
            if old is not None:
                shiboken6.delete(old)
            theme.clear_stylesheet()
            self._build_ui()
            self._build_actions()
            self.load_into_ui(self.profile, rebuild=True)
            theme.apply()  # styles the new tree once, in the new language
            self.dirty = dirty
            self.tabs.setCurrentIndex(tab)
            if 0 <= row < self.step_list.count():
                self.step_list.setCurrentRow(row)
            for stamp, text, level, kind in entries:
                self._add_log_line(stamp, text, level, kind)
            self._refresh_title()
            self._set_running(False)
        finally:
            self.setUpdatesEnabled(True)

    # ================================================================== logging

    def _log_format(self, level: str, kind: str) -> QTextCharFormat:
        key = {"warn": "lg_w", "error": "lg_e"}.get(level)
        if key is None:
            key = {"click": "lg_c", "match": "lg_m", "loop": "lg_l"}.get(kind, "lg_i")
        fmt = QTextCharFormat()
        fmt.setForeground(QColor(theme.tok(key)))
        if kind in ("start", "finished"):
            fmt.setForeground(QColor(theme.tok("lg_i")))  # start / finished lines stand out by colour, not by weight
        return fmt

    def append_log(self, text: str, level: str = "info", kind: str = "") -> None:
        stamp = dt.datetime.now().strftime("%H:%M:%S")
        self._log_entries.append((stamp, text, level, kind))
        del self._log_entries[:-3000]
        self._add_log_line(stamp, text, level, kind)
        log.log({"info": logging.INFO, "warn": logging.WARNING, "error": logging.ERROR}.get(level, logging.INFO), text)

    def _add_log_line(self, stamp: str, text: str, level: str, kind: str) -> None:
        cur = self.log_view.textCursor()
        cur.movePosition(QTextCursor.MoveOperation.End)
        ts = QTextCharFormat()
        ts.setForeground(QColor(theme.tok("lg_t")))
        cur.insertText(f"{stamp}  ", ts)
        cur.insertText(text + "\n", self._log_format(level, kind))
        self.log_view.setTextCursor(cur)
        self.log_view.ensureCursorVisible()
        self._log_n += 1
        self.log_count.setText(str(min(self._log_n, 3000)))
        self.log_last.setText(f"[{stamp}] {text}")

    # ================================================================== run events and dashboard

    @staticmethod
    def _new_stats() -> dict:
        return {"clicks": 0, "loops": 0, "t0": 0.0, "step": "", "step_no": 0, "countdown_until": 0.0, "started": False,
                "dry": False, "total": 0}

    def _on_event(self, ev: Event) -> None:
        text = i18n.format_event(ev) if i18n.language() == "zh" else ev.message
        self.append_log(text, ev.level, ev.kind)
        st = self._stats
        if ev.kind == "preset" and ev.data.get("preset"):
            # auto-detection picked a preset: reflect it in the UI (not a user edit, so not "dirty")
            self.profile.settings.active_preset = ev.data["preset"]
            self._refresh_presets()
        if ev.kind == "loop":
            st["loops"] = int(ev.data.get("loop", st["loops"] + 1))
            st["started"] = True
        elif ev.kind == "click":
            st["clicks"] += 1
        elif ev.kind == "step":
            st["started"] = True
            name = ev.data.get("step", "")
            st["step"] = name
            enabled = [s for s in self.profile.steps if s.enabled]
            for i, s in enumerate(enabled, 1):
                if s.name == name:
                    st["step_no"] = i
                    self.step_list.set_current_step(s.id)
                    break
        elif ev.kind == "match":
            self._show_match(ev)
        self._refresh_dashboard()
        if ev.kind == "match" and self._run_thread is not None and self._run_thread.runner.dry_run:
            rect = ev.data.get("rect")
            if rect and ev.data.get("tag") != "end":
                self.highlighter.flash(rect, f"{ev.data['confidence']:.2f}", ev.data.get("found", False), 500)

    def _show_match(self, ev: Event) -> None:
        d = ev.data
        found = bool(d.get("found"))
        conf = float(d.get("confidence", 0.0))
        step = next((s for s in self.profile.steps if s.name == d.get("step")), None)
        thr = step.threshold if step else 0.85
        self.match_tag.setVisible(True)
        self.match_tag.setText(tr("Found") if found else tr("Not found"))
        self.match_tag.setProperty("tag", "ok" if found else "wn")
        repolish(self.match_tag)
        self.match_name.setText(str(d.get("step", "")))
        self.match_val.setText(f"{conf:.2f} {'≥' if found else '<'} {thr:.2f}")
        self.match_meter.set(conf, thr)
        self.match_thr_lbl.setText(tr("Strictness {v:.2f}", v=thr))
        rect = d.get("rect")
        self.match_pos.setText(f"({rect[0] + rect[2] / 2:.0f}, {rect[1] + rect[3] / 2:.0f})" if rect and found else "")

    def _refresh_dashboard(self) -> None:
        st, s = self._stats, self.profile.settings
        n_steps = len([x for x in self.profile.steps if x.enabled])
        self.tile_clicks.set(str(st["clicks"]), (tr("Limit {n}", n=s.max_total_clicks) + f" · {st['clicks'] * 100 // s.max_total_clicks}%")
                             if s.max_total_clicks else tr("No limit"))
        self.tile_loops.set(str(st["loops"]), tr("Infinite") if s.infinite_loop else tr("of {n}", n=s.loop_count))
        self.tile_step.set(f"{st['step_no']} / {n_steps}" if st["step_no"] else f"– / {n_steps}", st["step"])
        if s.max_total_clicks:
            self.dash_progress.setVisible(True)
            self.dash_progress.setValue(min(1000, st["clicks"] * 1000 // s.max_total_clicks))
            self.dash_prog_l.setText(tr("Click progress (limit {n})", n=s.max_total_clicks))
            self.dash_prog_r.setText(f"{st['clicks']} / {s.max_total_clicks}")
        else:
            self.dash_progress.setVisible(False)
            self.dash_prog_l.setText(tr("No click limit"))
            self.dash_prog_r.setText(str(st["clicks"]))
        self._tick_dashboard()

    def _tick_dashboard(self) -> None:
        st = self._stats
        if not self._running:
            return
        now = time.monotonic()
        el = max(0, int(now - st["t0"]))
        avg = (now - st["t0"]) / st["clicks"] if st["clicks"] else 0.0
        self.tile_time.set(f"{el // 60:02d}:{el % 60:02d}", tr("Average {s:.1f} s per click", s=avg) if st["clicks"] else "")
        mode = tr("Dry run") if st["dry"] else tr("Running")
        if not st["started"] and now < st["countdown_until"]:
            left = math.ceil(st["countdown_until"] - now)
            self.pill.set_state("cdn", tr("Starting in {n} s", n=left))
            self.dash_pill.set_state("cdn", tr("Starting in {n} s", n=left))
            self.dash_text.setText(tr("Switch to your target window now."))
            self.stop_badge.set_status(tr("Starting in {n} s", n=left))
        else:
            self.pill.set_state("dry" if st["dry"] else "run", mode)
            self.dash_pill.set_state("dry" if st["dry"] else "run", mode)
            self.dash_text.setText(tr("Working through {n} step(s) in order", n=st["total"]))
            self.stop_badge.set_status(tr("{mode} \u00b7 {n} clicks", mode=mode, n=st["clicks"]))

    # ================================================================== profile <-> UI

    def load_into_ui(self, profile: Profile, rebuild: bool = False) -> None:
        self._building = True
        try:
            self.profile = profile
            self.step_list.clear()
            for s in profile.steps:
                self.step_list.addItem(self._make_item(s))
            s = profile.settings
            self.s_loops.setValue(s.loop_count)
            self.s_infinite.setChecked(s.infinite_loop)
            self.s_jitter.setValue(s.jitter_percent)
            self.s_maxclicks.setValue(s.max_total_clicks)
            if s.max_total_clicks:
                self._last_max_clicks = s.max_total_clicks
            self.max_row.switch.setChecked(s.max_total_clicks > 0)
            self.s_hotkey.setText(s.stop_hotkey)
            self.s_start_delay.setValue(s.start_delay)
            self.s_scan.setValue(s.scan_interval)
            self.s_corner.setChecked(s.corner_abort)
            self.s_minimize.setChecked(s.minimize_on_run)
            self.s_idle.setValue(s.max_idle_loops)
            self.s_loops.setEnabled(not s.infinite_loop)
            self.speed_slider.setValue(s.speed)
            self.chk_manual.setChecked(s.speed_manual)
            self.notes.setPlainText(profile.notes)
            if profile.end_condition is None:
                profile.end_condition = Step(name="End condition", enabled=False)
            self._refresh_presets()
            self.end_editor.set_step(profile.end_condition, profile.directory, self.active_preset)
        finally:
            self._building = False
        self.dirty = False
        if profile.directory:
            storage.remember_last_profile(profile.directory)
        self._refresh_banner()
        self._refresh_title()
        if self.step_list.count():
            self.step_list.setCurrentRow(0)
        else:
            self.step_editor.set_step(None, None)
        self._refresh_displays_label()  # also fills the display chooser from the monitor list
        self._set_monitor_combo(s.monitor)
        self._refresh_speed_ui()
        self._refresh_empty()
        if self._hotkey_enabled and not rebuild and not self._defer_hotkey:
            self._start_hotkey()

    def _set_monitor_combo(self, idx: int) -> None:
        i = self.s_monitor.findData(idx)
        if i < 0:
            self.s_monitor.blockSignals(True)
            self.s_monitor.addItem(tr("Screen {n}", n=idx), idx)
            self.s_monitor.blockSignals(False)
            i = self.s_monitor.findData(idx)
        self.s_monitor.blockSignals(True)
        self.s_monitor.setCurrentIndex(i)
        self.s_monitor.blockSignals(False)

    def _fill_monitor_combo(self, mons) -> None:
        keep = self.s_monitor.currentData()
        self.s_monitor.blockSignals(True)
        self.s_monitor.clear()
        self.s_monitor.addItem(tr("All screens"), 0)
        for m in mons:
            self.s_monitor.addItem(f"{tr('Screen {n}', n=m.index)}{' ' + tr('(primary)') if m.primary else ''}  {m.width}\u00d7{m.height}",
                                   m.index)
        self.s_monitor.blockSignals(False)
        want = keep if keep is not None else self.profile.settings.monitor
        self._set_monitor_combo(want if want is not None else 0)

    def _refresh_empty(self) -> None:
        """Empty profile: show the welcome screen; otherwise the workspace. Also updates counters and the dock hint."""
        has = bool(self.profile.steps)
        if not self._running:
            self.main_stack.setCurrentIndex(0 if has else 1)
        self.step_count.setText(str(len(self.profile.steps)))
        self.btn_start.setEnabled(self._running or has)
        self.btn_dry.setEnabled(has and not self._running)
        self.act_run.setEnabled(has and not self._running)
        self.act_dry.setEnabled(has and not self._running)
        self._refresh_dock_hint()

    def _refresh_dock_hint(self) -> None:
        key = self.profile.settings.stop_hotkey or "F8"
        if not self.profile.steps and not self._running:
            self.dock_hint.setText(tr("Add a step to get started"))
        else:
            self.dock_hint.setText(tr("Stop any time: {key}, or a screen corner.", key=key))

    _TAGS = None

    def _thumb_for(self, step: Step) -> Optional[QPixmap]:
        rel = effective_template(step, self.active_preset)
        if not rel or self.profile.directory is None:
            return None
        path = Path(self.profile.directory) / rel
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return None
        hit = self._thumb_cache.get(str(path))
        if hit and hit[0] == mtime:
            return hit[1]
        try:
            pix = bgr_to_pixmap(storage.read_image(path))
        except storage.ProfileError:
            return None
        pix = pix.scaled(88, 88, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
        self._thumb_cache[str(path)] = (mtime, pix)
        return pix

    def _make_item(self, step: Step) -> QListWidgetItem:
        item = QListWidgetItem()
        item.setData(ROLE_ID, step.id)
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsDragEnabled
                      | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled)
        item.setCheckState(Qt.CheckState.Checked if step.enabled else Qt.CheckState.Unchecked)
        item.setSizeHint(QSize(0, ROW_H))
        self._style_item(item, step)
        return item

    def _style_item(self, item: QListWidgetItem, step: Step) -> None:
        idx = self.profile.steps.index(step) + 1 if step in self.profile.steps else 0
        tags: list[tuple[str, str]] = []
        tpl = effective_template(step, self.active_preset)
        if not tpl:
            tags.append((tr("No target"), "er"))
        elif effective_region(step, self.active_preset) is None:
            tags.append((tr("No search area"), ""))  # (advice, not an error: shown in the editor, not on the row)
        if step.click_only_if_found:
            tags.append((tr("Optional"), ""))
        if step.ignore_background:
            tags.append((tr("Outline"), ""))
        if step.fallback_click_last:
            tags.append((tr("Fallback click"), "" if step.guard_template else "wn"))
        if step.click_type != "left":
            tags.append((tr("Right-click") if step.click_type == "right" else tr("Double-click"), "wn"))
        suffix = f"   [{', '.join(t for t, _ in tags)}]" if tags else ""
        item.setText(f"{idx}. {step.name}{suffix}")  # also the accessible / searchable text (the delegate paints the card)
        item.setData(ROLE_VIEW, {"index": idx, "name": step.name, "tags": tags, "thumb": self._thumb_for(step),
                                 "enabled": step.enabled, "hidden": self._running})

    def _refresh_items(self) -> None:
        self._building = True
        try:
            for i, s in enumerate(self.profile.steps):
                item = self.step_list.item(i)
                if item:
                    self._style_item(item, s)
                    item.setCheckState(Qt.CheckState.Checked if s.enabled else Qt.CheckState.Unchecked)
        finally:
            self._building = False
        self.step_list.viewport().update()

    def _refresh_banner(self) -> None:
        notes = self.profile.notes.strip()
        first = notes if len(notes) <= 420 else notes[:420] + "..."
        self.banner.setText(first)

    def _show_notes(self) -> None:
        dialogs.message(self, "if", tr("Profile notes"), self.profile.notes.strip())

    def _refresh_title(self) -> None:
        star = " *" if self.dirty else ""
        self.setWindowTitle(f"ClickFlow - {self.profile.name}{star}")
        self.profile_btn.setText(self.profile.name)
        # unsaved changes: a coral dot on the profile name (nothing at all when everything is saved)
        self.profile_btn.setIcon(self._dot_icon() if self.dirty else QIcon())
        where = str(self.profile.directory) if self.profile.directory else tr("unsaved profile")
        self.profile_btn.setToolTip(where + ("\n" + tr("Unsaved changes") if self.dirty else ""))

    def _mark_dirty(self) -> None:
        if self._building:
            return
        self.dirty = True
        self._refresh_title()

    @property
    def active_preset(self) -> Optional[Preset]:
        return self.profile.preset_by_id(self.profile.settings.active_preset)

    def _refresh_speed_ui(self) -> None:
        """Reflect profile.settings.speed / speed_manual in the speed controls and lock/unlock the per-step waits."""
        s = self.profile.settings
        manual = s.speed_manual
        self.speed_slider.setEnabled(not manual)
        self.speed_seg.setEnabled(not manual)
        self.lbl_slow.setEnabled(not manual)
        self.lbl_fast.setEnabled(not manual)
        self._syncing_speed = True
        try:
            i = self.speed_seg.findData(s.speed)
            if i >= 0:
                self.speed_seg.setCurrentIndex(i)
            else:
                self.speed_seg.clearSelection()
        finally:
            self._syncing_speed = False
        if manual:
            self.lbl_speed.setText(tr("Manual: every step uses its own waits (Advanced > Waits & failure handling)"))
        else:
            iv = click_interval(s.speed)
            if iv <= 0:
                self.lbl_speed.setText(tr("Fastest: no added delay (about {d:.2f} s between clicks)", d=display_interval(s.speed)))
            else:
                self.lbl_speed.setText(tr("At least {iv:.2f} s between clicks", iv=iv))
        self.step_editor.set_waits_locked(not manual, FAST_WAIT, FAST_WAIT)
        self._refresh_repeat_hint()

    def _show_step(self, step: Optional[Step]) -> None:
        self.step_editor.set_step(step, self.profile.directory if step else None, self.active_preset)
        self.step_editor.set_waits_locked(not self.profile.settings.speed_manual, FAST_WAIT, FAST_WAIT)

    def current_step(self) -> Optional[Step]:
        row = self.step_list.currentRow()
        return self.profile.steps[row] if 0 <= row < len(self.profile.steps) else None

    # ---- list/editor callbacks

    def _on_row_changed(self, row: int) -> None:
        if self._building:
            return
        step = self.current_step()
        self._show_step(step)

    def _on_item_changed(self, item: QListWidgetItem) -> None:
        if self._building:
            return
        row = self.step_list.row(item)
        if 0 <= row < len(self.profile.steps):
            enabled = item.checkState() == Qt.CheckState.Checked
            if self.profile.steps[row].enabled != enabled:
                self.profile.steps[row].enabled = enabled
                self._mark_dirty()
                self._refresh_items()  # the row is painted from its view data, so refresh it (switch, dimmed thumbnail)
                if row == self.step_list.currentRow():
                    self._show_step(self.profile.steps[row])

    def _on_rows_moved(self, *_a) -> None:
        if self._building:
            return
        by_id = {s.id: s for s in self.profile.steps}
        order = [self.step_list.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.step_list.count())]
        if sorted(order) == sorted(by_id):
            self.profile.steps = [by_id[i] for i in order]
            self._mark_dirty()
            QTimer.singleShot(0, self._refresh_items)

    def _on_step_edited(self) -> None:
        self._mark_dirty()
        self._refresh_items()

    def _on_notes_changed(self) -> None:
        if self._building:
            return
        self.profile.notes = self.notes.toPlainText()
        self._mark_dirty()
        self._refresh_banner()

    # ---- step list operations

    def add_step(self) -> None:
        s = Step(name=f"Step {len(self.profile.steps) + 1}")  # noqa
        self.profile.steps.append(s)
        self.step_list.addItem(self._make_item(s))
        self.step_list.setCurrentRow(self.step_list.count() - 1)
        self._mark_dirty()
        self._refresh_items()
        self._refresh_empty()

    def add_step_and_select(self) -> None:
        """The *Add step* button: a new step and straight into the screen selection (Esc removes the empty step again)."""
        self.add_step()
        self._pending_new_step = self.profile.steps[-1].id
        self.capture_step_template()

    def _drop_pending_empty_step(self) -> None:
        """The first capture of a step created by *Add step* was cancelled: remove that step again if it is still empty."""
        pend, self._pending_new_step = self._pending_new_step, None
        if not pend:
            return
        for i, st in enumerate(self.profile.steps):
            if st.id == pend and not st.template:
                self.step_list.setCurrentRow(i)
                self.delete_step()
                return

    def delete_step(self) -> None:
        row = self.step_list.currentRow()
        if row < 0:
            return
        self.profile.remove_step_refs(self.profile.steps[row].id)
        del self.profile.steps[row]
        self._building = True
        self.step_list.takeItem(row)
        self._building = False
        self._mark_dirty()
        self._refresh_items()
        if self.step_list.count():
            self.step_list.setCurrentRow(min(row, self.step_list.count() - 1))
        else:
            self.step_editor.set_step(None, None)
        self._refresh_empty()

    def duplicate_step(self) -> None:
        step = self.current_step()
        if step is None:
            return
        row = self.step_list.currentRow()
        dup = Step.from_dict(step.to_dict())
        dup.id = Step().id
        dup.name = f"{step.name} copy"
        if self.profile.directory:
            storage.copy_template(self.profile.directory, step, dup)
            storage.copy_preset_entries(self.profile.directory, self.profile, step, dup)
        self.profile.steps.insert(row + 1, dup)
        self.step_list.insertItem(row + 1, self._make_item(dup))
        self.step_list.setCurrentRow(row + 1)
        self._mark_dirty()
        self._refresh_items()

    def move_step(self, delta: int) -> None:
        row = self.step_list.currentRow()
        new = row + delta
        if row < 0 or not (0 <= new < len(self.profile.steps)):
            return
        steps = self.profile.steps
        steps[row], steps[new] = steps[new], steps[row]
        self._building = True
        item = self.step_list.takeItem(row)
        self.step_list.insertItem(new, item)
        self._building = False
        self.step_list.setCurrentRow(new)
        self._mark_dirty()
        self._refresh_items()

    # ---- settings

    def _apply_settings(self, *_a) -> None:
        if self._building:
            return
        s = self.profile.settings
        s.loop_count = self.s_loops.value()
        s.infinite_loop = self.s_infinite.isChecked()
        s.jitter_percent = self.s_jitter.value()
        s.max_total_clicks = self.s_maxclicks.value()
        if s.max_total_clicks:
            self._last_max_clicks = s.max_total_clicks
        s.start_delay = self.s_start_delay.value()
        s.scan_interval = self.s_scan.value()
        s.corner_abort = self.s_corner.isChecked()
        s.minimize_on_run = self.s_minimize.isChecked()
        s.max_idle_loops = self.s_idle.value()
        mon = self.s_monitor.currentData()
        s.monitor = int(mon) if mon is not None else s.monitor
        s.auto_preset = self.chk_auto_preset.isChecked()
        s.speed = self.speed_slider.value()
        s.speed_manual = self.chk_manual.isChecked()
        self._refresh_speed_ui()
        self.s_loops.setEnabled(not s.infinite_loop)
        self.max_row.switch.blockSignals(True)
        self.max_row.switch.setChecked(s.max_total_clicks > 0)
        self.max_row.switch.blockSignals(False)
        self._refresh_repeat_hint()
        self._mark_dirty()

    def _on_max_switch(self, on: bool) -> None:
        if self._building:
            return
        if on:
            self.s_maxclicks.setValue(self._last_max_clicks or 500)
        else:
            self._last_max_clicks = self.s_maxclicks.value() or self._last_max_clicks
            self.s_maxclicks.setValue(0)

    def _refresh_repeat_hint(self) -> None:
        s = self.profile.settings
        self.lbl_repeat.setText(tr("Runs until you stop it") if s.infinite_loop
                                else tr("Stops automatically after {n} loop(s)", n=s.loop_count))

    def _on_speed_preset(self, i: int) -> None:
        if self._building or getattr(self, "_syncing_speed", False):
            return
        v = self.speed_seg.currentData()
        if v is not None:
            self.speed_slider.setValue(int(v))

    def _apply_hotkey(self) -> None:
        text = self.s_hotkey.text().strip()
        try:
            parse_hotkey(text)
        except ValueError as exc:
            dialogs.warn(self, tr("Invalid hotkey"), str(exc))
            self.s_hotkey.setText(self.profile.settings.stop_hotkey)
            return
        if text != self.profile.settings.stop_hotkey:
            self.profile.settings.stop_hotkey = text
            self._mark_dirty()
            self._start_hotkey()
            self._refresh_dock_hint()

    def _start_hotkey(self) -> None:
        """Two independent stop-key mechanisms: a polling watcher (Windows) and a pynput global hotkey."""
        if not self._hotkey_enabled:
            return
        key = self.profile.settings.stop_hotkey or "F8"
        self.hotkey.hotkey = key
        self.key_watcher.hotkey = key
        polling = self.key_watcher.start()
        hooked = self.hotkey.start()
        if polling or hooked:
            how = " + ".join(n for n, ok in (("key-state polling", polling), ("global hook", hooked)) if ok)
            self.append_log(f"Stop hotkey active: {key} ({how}). Also: red STOP button on screen, "
                            "or move the mouse into a screen corner.", "info")
        else:
            self.append_log(f"Could not register stop hotkey '{key}': {self.hotkey.error}. "
                            "Use the red STOP button or the screen-corner emergency stop.", "warn")

    # ================================================================== display presets

    def _detect_monitors(self) -> list[MonitorInfo]:
        try:
            self._monitors = MssScreen(0).monitors()
        except Exception as exc:  # no display / permission
            self._monitors = []
            self.append_log(f"Could not detect displays: {exc}", "warn")
        return self._monitors

    def _refresh_displays_label(self) -> None:
        mons = self._detect_monitors()
        if not mons:
            self.lbl_displays.setText(tr("Detected displays: none (screen capture unavailable)"))
            self._fill_monitor_combo([])
            return
        parts = []
        for m in mons:
            match = [p.name for p in self.profile.presets if p.resolution == m.resolution]
            parts.append(f"{m.label}" + (f" -> preset '{', '.join(match)}'" if match else " -> no preset"))
        self.lbl_displays.setText(tr("Detected displays: {x}", x="   |   ".join(parts)))
        self._fill_monitor_combo(mons)

    def _refresh_presets(self) -> None:
        """Rebuild the preset combo from the profile (signals blocked, so this never marks the profile dirty)."""
        s = self.profile.settings
        self.preset_combo.blockSignals(True)
        self.chk_auto_preset.blockSignals(True)
        try:
            self.preset_combo.clear()
            self.preset_combo.addItem(tr("Default (no preset)"), "")
            for p in self.profile.presets:
                self.preset_combo.addItem(p.label, p.id)
            idx = self.preset_combo.findData(s.active_preset)
            if idx < 0:
                s.active_preset = ""
                idx = 0
            self.preset_combo.setCurrentIndex(idx)
            self.chk_auto_preset.setChecked(s.auto_preset)
        finally:
            self.preset_combo.blockSignals(False)
            self.chk_auto_preset.blockSignals(False)
        has = self.active_preset is not None
        self.btn_preset_rename.setEnabled(has)
        self.btn_preset_delete.setEnabled(has)
        self._refresh_preset_btn()

    def _refresh_preset_btn(self) -> None:
        p = self.active_preset
        name = p.name if p else tr("Default")
        self.preset_menu.setTitle(f"{tr('Display preset')}: {name}")
        self.preset_btn.setText(name)
        self.preset_btn.setToolTip(f"{tr('Display preset')}: {name}")

    def _fill_preset_menu(self) -> None:
        """Top-bar preset menu: pick a preset, create / rename / delete one, auto-switch, re-detect displays."""
        m = self.preset_menu
        m.clear()
        m.addSection(tr("Display preset"))
        cur = self.profile.settings.active_preset
        for pid, text in [("", tr("Default (no preset)"))] + [(p.id, p.label) for p in self.profile.presets]:
            a = m.addAction(text)
            a.setCheckable(True)
            a.setChecked(pid == cur)
            a.triggered.connect(lambda _=False, i=pid: self._choose_preset(i))
        m.addSeparator()
        m.addAction(tr("New preset") + "...", self.new_preset)
        has = self.active_preset is not None
        m.addAction(tr("Rename preset") + "...", self.rename_preset).setEnabled(has)
        m.addAction(tr("Delete preset") + "...", self.delete_preset).setEnabled(has)
        m.addSeparator()
        auto = m.addAction(tr("Auto-switch by detected resolution"))
        auto.setCheckable(True)
        auto.setChecked(self.chk_auto_preset.isChecked())
        auto.triggered.connect(lambda on: self.chk_auto_preset.setChecked(bool(on)))
        m.addAction(tr("Detect again"), self._refresh_displays_label)
        m.addAction(tr("More settings..."), self._open_global_tab)

    def _choose_preset(self, preset_id: str) -> None:
        i = self.preset_combo.findData(preset_id)
        if i >= 0:
            self.preset_combo.setCurrentIndex(i)  # same path as the combo box in Global settings

    def _on_preset_selected(self, idx: int) -> None:
        if self._building:
            return
        self.profile.settings.active_preset = self.preset_combo.itemData(idx) or ""
        self._mark_dirty()
        self._refresh_for_preset()

    def _refresh_for_preset(self) -> None:
        """The selected preset changed: show its boxes/templates in the editors and the step list."""
        self._refresh_presets()
        self._show_step(self.current_step())
        self.end_editor.set_step(self.profile.end_condition, self.profile.directory, self.active_preset)
        self._refresh_items()

    def create_preset(self, name: str, monitor: MonitorInfo) -> Preset:
        preset = Preset(name=name.strip() or monitor.label, resolution=monitor.resolution, monitor=monitor.index)
        self.profile.presets.append(preset)
        self.profile.settings.active_preset = preset.id
        self._mark_dirty()
        self._refresh_for_preset()
        self._refresh_displays_label()
        return preset

    def new_preset(self) -> None:
        mons = self._detect_monitors()
        if not mons:
            dialogs.warn(self, tr("No displays"), tr("Could not detect any display (is screen capture allowed?)."))
            return
        mon = mons[0]
        if len(mons) > 1:
            labels = [m.label for m in mons]
            choice = dialogs.choose(self, tr("New preset"), tr("Which display is this preset for?"), labels)
            if choice is None:
                return
            mon = mons[choice]
        res = dialogs.ask_text(self, tr("New preset"), tr("A preset keeps its own search areas for one screen resolution."),
                               tr("Preset name"), f"{mon.width}x{mon.height}", tr("Create"))
        if res is None:
            return
        preset = self.create_preset(res[0], mon)
        self.append_log(f"Created preset '{preset.name}' for {mon.label}. It starts without search areas: select each step "
                        "and press 'Draw' on this display (targets fall back to the default ones; "
                        "re-select them if the icons look different here).", "info")

    def rename_preset(self) -> None:
        p = self.active_preset
        if p is None:
            return
        res = dialogs.ask_text(self, tr("Rename preset"), "", tr("Preset name"), p.name, tr("Rename"))
        if res and res[0].strip():
            p.name = res[0].strip()
            self._mark_dirty()
            self._refresh_for_preset()
            self._refresh_displays_label()

    def remove_preset(self, preset: Preset) -> None:
        for rel in preset.templates.values():  # the preset's own template files go with it
            try:
                (Path(self.profile.directory) / rel).unlink(missing_ok=True)
            except (OSError, TypeError):
                pass
        self.profile.presets = [p for p in self.profile.presets if p.id != preset.id]
        if self.profile.settings.active_preset == preset.id:
            self.profile.settings.active_preset = ""
        self._mark_dirty()
        self._refresh_for_preset()
        self._refresh_displays_label()

    def delete_preset(self) -> None:
        p = self.active_preset
        if p is None:
            return
        if dialogs.confirm(self, tr("Delete preset"), tr("Delete preset \"{name}\" and its search areas?", name=p.name),
                           tr("Delete"), danger=True):
            self.remove_preset(p)

    # ================================================================== file operations

    def _confirm_discard(self) -> bool:
        if not self.dirty:
            return True
        choice = dialogs.confirm_unsaved(self, self.profile.name)
        if choice == "cancel":
            return False
        if choice == "save":
            return self.save()
        return True

    def _ensure_dir(self) -> bool:
        if self.profile.directory is None:
            self.profile.directory = storage.new_profile(self.profile.name).directory
        return True

    def new_profile(self) -> None:
        if not self._confirm_discard():
            return
        res = dialogs.ask_text(self, tr("New profile"), tr("A profile stores a set of steps and settings."), tr("Profile name"),
                               tr("My profile"), tr("Create"),
                               starts=[("blank", tr("Blank")), ("example", tr("Example"))])
        if res is None or not res[0].strip():
            return
        if res[1] == "example":
            self.dirty = False
            self.open_example()
            return
        self.load_into_ui(storage.new_profile(res[0].strip()))

    def open_profile(self) -> None:
        if not self._confirm_discard():
            return
        start = str(storage.profiles_root())
        Path(start).mkdir(parents=True, exist_ok=True)
        path, _ = QFileDialog.getOpenFileName(self, tr("Open profile"), start, "ClickFlow profile (profile.json);;JSON (*.json)")
        if path:
            self.open_path(path)

    def open_path(self, path) -> bool:
        try:
            self.load_into_ui(storage.load_profile(path))
        except storage.ProfileError as exc:
            dialogs.error(self, tr("Cannot open profile"), str(exc))
            return False
        self.append_log(f"Loaded profile '{self.profile.name}' from {self.profile.directory}")
        if self.profile.notes.strip():
            self.append_log("NOTE: " + self.profile.notes.strip().splitlines()[0], "warn")
        return True

    def open_example(self) -> None:
        from ..app import ensure_example

        if not self._confirm_discard():
            return
        path = ensure_example()
        if path:
            self.open_path(path)
        else:
            dialogs.warn(self, tr("Example missing"), tr("The bundled example profile could not be found."))

    def _flush_pending_edits(self) -> None:
        """Commit anything the user typed but has not confirmed yet (Enter / Tab / focus-out).

        Toolbar buttons and menu actions do not take keyboard focus, so a value typed into a field
        would otherwise still be sitting in the editor when Save / Start reads the profile.
        """
        for spin in self.findChildren(QAbstractSpinBox):
            spin.interpretText()  # emits valueChanged if the typed text differs
        # Text fields only report "editing finished" on a focus-out event, which the OS may never deliver
        # (e.g. the window is not the active one). So read every editor explicitly instead of relying on focus.
        self.step_editor.commit()
        self.end_editor.commit()
        self._apply_settings()
        text = self.s_hotkey.text().strip()
        if text != self.profile.settings.stop_hotkey:
            self._apply_hotkey()

    def save(self) -> bool:
        self._flush_pending_edits()
        try:
            self._ensure_dir()
            self.profile.notes = self.notes.toPlainText()
            storage.save_profile(self.profile)
        except (OSError, storage.ProfileError) as exc:
            dialogs.error(self, tr("Save failed"), str(exc))
            return False
        self.dirty = False
        self._refresh_title()
        self.append_log(f"Saved profile to {self.profile.directory}")
        return True

    def save_as(self) -> None:
        res = dialogs.ask_text(self, tr("Save as..."), "", tr("New profile name"), self.profile.name + " copy", tr("Save"))
        if res is None or not res[0].strip():
            return
        name = res[0]
        folder = storage.profiles_root() / storage.slugify(name.strip())
        if folder.exists():
            dialogs.warn(self, tr("Already exists"), tr("A profile folder named \"{n}\" already exists.", n=folder.name))
            return
        self.profile.name = name.strip()
        try:
            storage.save_profile(self.profile, folder)
        except (OSError, storage.ProfileError) as exc:
            dialogs.error(self, tr("Save failed"), str(exc))
            return
        self.dirty = False
        self.end_editor.set_step(self.profile.end_condition, self.profile.directory, self.active_preset)
        self._refresh_title()
        self.append_log(f"Saved profile as {self.profile.directory}")

    def export_zip(self) -> None:
        if not self.save():
            return
        default = str(Path.home() / f"{storage.slugify(self.profile.name)}.zip")
        path, _ = QFileDialog.getSaveFileName(self, tr("Export profile"), default, "Zip archive (*.zip)")
        if path:
            try:
                storage.export_profile(self.profile, path)
                self.append_log(f"Exported profile to {path}")
            except (OSError, storage.ProfileError) as exc:
                dialogs.error(self, tr("Export failed"), str(exc))

    def import_zip(self) -> None:
        if not self._confirm_discard():
            return
        path, _ = QFileDialog.getOpenFileName(self, tr("Import profile"), str(Path.home()), "Zip archive (*.zip)")
        if path:
            try:
                self.load_into_ui(storage.import_profile(path))
                self.append_log(f"Imported profile '{self.profile.name}'")
            except Exception as exc:
                dialogs.error(self, tr("Import failed"), str(exc))

    def show_about(self, start_tab: int = 0) -> None:
        from .. import __version__
        from ..app import resource_root

        lics = []
        for title, name in (("ClickFlow (MIT)", "LICENSE"), ("Third-party notices", "THIRD_PARTY_NOTICES.md")):
            try:
                lics.append((title, (resource_root() / name).read_text(encoding="utf-8")))
            except OSError:
                lics.append((title, f"{name} was not found next to the program."))
        dialogs.about(self, __version__, lics, start_tab if isinstance(start_tab, int) else 0)

    def show_licenses(self) -> None:
        """LICENSE and THIRD_PARTY_NOTICES.md (bundled inside the exe as well)."""
        self.show_about(1)

    # ================================================================== capture

    def _capture_monitor_info(self) -> Optional[MonitorInfo]:
        """The monitor the active preset applies to (None for the default configuration)."""
        preset = self.active_preset
        if preset is None:
            return None
        return presets_mod.resolve_explicit(preset, self._detect_monitors()).monitor

    def _capture_monitor(self) -> int:
        """mss monitor index used for capturing: the active preset's monitor, else the global setting."""
        info = self._capture_monitor_info()
        return info.index if info else self.profile.settings.monitor

    def _qt_geometry(self, frame, monitor_index: int) -> QRect:
        """Qt screen rectangle (logical units) that corresponds to the captured frame."""
        screens = QGuiApplication.screens()
        primary = QGuiApplication.primaryScreen()
        if monitor_index == 0 or not screens:
            return primary.virtualGeometry()
        ih, iw = frame.image.shape[:2]
        best, best_key = None, None
        for sc in screens:
            g, dpr = sc.geometry(), sc.devicePixelRatio()
            key = (abs(g.width() * dpr - iw) + abs(g.height() * dpr - ih),
                   abs(g.x() * dpr - frame.left) + abs(g.y() * dpr - frame.top))
            if best_key is None or key < best_key:
                best, best_key = sc, key
        if best is None or best_key[0] > 60:  # sizes disagree: fall back to the enumeration order
            best = screens[monitor_index - 1] if 0 < monitor_index <= len(screens) else primary
        return best.geometry()

    def _begin_capture(self, hint: str, on_rect, guides=None) -> None:
        """Hide the window, screenshot the screen, show the drag overlay; call on_rect(frame, x, y, w, h).

        ``guides(frame)`` may return boxes to draw for orientation. ``on_rect`` may return
        ``(hint, on_rect, guides)`` to chain a second drag on the same screenshot.
        """
        if self._overlay is not None:
            return
        if not self._ensure_permissions():
            return
        mon = self._capture_monitor()
        self.showMinimized()

        def snap():
            try:
                frame = MssScreen(mon).grab()
            except Exception as exc:
                self._restore_window()
                dialogs.error(self, tr("Screen capture failed"), str(exc))
                return
            self._show_overlay(frame, mon, hint, on_rect, guides)

        QTimer.singleShot(450, snap)

    def _show_overlay(self, frame, mon: int, hint: str, on_rect, guides=None) -> None:
        kind, self._capture_kind = self._capture_kind, "target"
        ov = CaptureOverlay(frame.image, self._qt_geometry(frame, mon), hint, guides=guides(frame) if guides else [], kind=kind)
        self._overlay = ov
        ov.selected.connect(lambda x, y, w, h: self._capture_done(on_rect, frame, mon, x, y, w, h))
        ov.cancelled.connect(self._capture_cancelled)
        ov.open()

    def _capture_done(self, on_rect, frame, mon: int, x, y, w, h) -> None:
        self._overlay = None
        self._pending_new_step = None
        follow = None
        try:
            follow = on_rect(frame, x, y, w, h)
        finally:
            if not follow:
                self._restore_window()
        if follow:
            hint, next_cb, guides = follow
            self._capture_kind = "area"  # the second drag of "Re-select" is the search area
            self._show_overlay(frame, mon, hint, next_cb, guides)

    def _capture_cancelled(self) -> None:
        self._overlay = None
        self._restore_window()
        self._drop_pending_empty_step()

    def _restore_window(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _box_from_frame(self, frame, x, y, w, h) -> tuple[int, int, int, int]:
        """Overlay selection (screenshot pixels) -> search box in the units the active config stores.

        Default config: global screen coordinates. Preset: relative to the monitor's top-left corner.
        """
        g = frame.rect_to_screen(x, y, w, h).as_tuple()
        if self.active_preset is not None:
            return (g[0] - int(round(frame.left)), g[1] - int(round(frame.top)), g[2], g[3])
        return g

    def _guides_for(self, frame, exclude: Optional[Step] = None) -> list[tuple[int, int, int, int, str]]:
        """Existing search boxes of the other steps, in screenshot pixels, so the user sees what is already covered."""
        out = []
        steps = [s for s in self.profile.steps if s is not exclude and s.enabled]
        for st in steps:
            box = presets_mod.resolve_region(st, self.active_preset, frame)
            img = frame.region_to_image(box) if box else None
            if img:
                out.append((img[0], img[1], img[2] - img[0], img[3] - img[1], st.name))
        return out

    def _save_crop(self, step: Step, frame, x, y, w, h) -> None:
        self._ensure_dir()
        crop = frame.image[y:y + h, x:x + w].copy()
        preset = self.active_preset
        rel = storage.save_template(self.profile.directory, step, crop, preset=preset)
        where = f" (preset '{preset.name}')" if preset else ""
        self.append_log(f"Captured target for '{step.name}'{where}: {w}x{h} px -> {rel}")
        self._mark_dirty()

    def _set_box(self, step: Step, frame, x, y, w, h) -> None:
        box = self._box_from_frame(frame, x, y, w, h)
        set_region(step, self.active_preset, box)
        preset = self.active_preset
        where = f" (preset '{preset.name}')" if preset else ""
        self.append_log(f"Search box for '{step.name}'{where}: x={box[0]} y={box[1]} w={box[2]} h={box[3]} - "
                        "applied automatically on every run.")
        self._mark_dirty()

    def capture_step_template(self) -> None:
        step = self.current_step()
        if step is None:
            return

        def box_done(frame, x, y, w, h):
            self._set_box(step, frame, x, y, w, h)
            self._show_step(step)
            self._refresh_items()

        def done(frame, x, y, w, h):
            self._save_crop(step, frame, x, y, w, h)
            self._show_step(step)
            self._refresh_items()
            if effective_region(step, self.active_preset) is None:
                # No search box yet: continue straight on the same screenshot (Esc skips = whole screen).
                return (tr("Now drag the SEARCH AREA: where this target appears (a bit bigger than the target). "
                           "Esc / right-click = skip, scan the whole screen."),
                        box_done, lambda fr: [(x, y, w, h, "template")] + self._guides_for(fr, step))

        self._begin_capture(tr("Drag a rectangle around the target. Esc / right-click cancels."), done,
                            lambda fr: self._guides_for(fr, step))

    def capture_step_guard(self) -> None:
        """Capture the step's "skip if this image is there" image (e.g. the red, already-liked heart)."""
        step = self.current_step()
        if step is None:
            return

        def done(frame, x, y, w, h):
            self._ensure_dir()
            crop = frame.image[y:y + h, x:x + w].copy()
            rel = storage.save_guard_template(self.profile.directory, step, crop)
            self.append_log(f"Captured 'skip if present' image for '{step.name}': {w}x{h} px -> {rel}")
            self._mark_dirty()
            self.step_editor.refresh_guard()

        self._begin_capture(tr("Drag a rectangle around the image that means 'already done' (e.g. the red, liked "
                               "heart). Esc / right-click cancels."), done, lambda fr: self._guides_for(fr, step))

    def pick_step_region(self) -> None:
        step = self.current_step()
        if step is None:
            return

        def done(frame, x, y, w, h):
            self._set_box(step, frame, x, y, w, h)
            self._show_step(step)
            self._refresh_items()

        self._capture_kind = "area"
        self._begin_capture(tr("Drag the SEARCH AREA: where this target appears. Esc / right-click cancels."), done,
                            lambda fr: self._guides_for(fr, step))

    def capture_end_template(self) -> None:
        step = self.profile.end_condition

        def done(frame, x, y, w, h):
            self._save_crop(step, frame, x, y, w, h)
            self.end_editor.set_step(step, self.profile.directory, self.active_preset)

        self._begin_capture(tr("Drag a rectangle around the image that means 'finished'."), done)

    def pick_end_region(self) -> None:
        step = self.profile.end_condition

        def done(frame, x, y, w, h):
            self._set_box(step, frame, x, y, w, h)
            self.end_editor.set_step(step, self.profile.directory, self.active_preset)

        self._capture_kind = "area"
        self._begin_capture(tr("Drag the area where the end image shows up."), done,
                            lambda fr: self._guides_for(fr))

    def show_search_boxes(self) -> None:
        """Briefly draw every search box (blue, dashed) on the real screen."""
        preset = self.active_preset
        mon = self._capture_monitor_info()
        ox, oy = (mon.left, mon.top) if (preset is not None and mon is not None) else (0, 0)
        steps = [s for s in self.profile.steps if s.enabled]
        if self.profile.end_condition and self.profile.end_condition.enabled:
            steps.append(self.profile.end_condition)
        shown = 0
        for st in steps:
            r = effective_region(st, preset)
            if r:
                rect = (r[0] + ox, r[1] + oy, r[2], r[3]) if preset is not None else r
                self.highlighter.flash(rect, st.name, False, 3500, kind="region")
                shown += 1
        if shown:
            self.append_log(f"Showing {shown} search box(es) on screen for a few seconds (blue, dashed).")
        else:
            self.append_log("No search boxes are set yet - select a step and press 'Draw search box...'.", "warn")

    # ================================================================== test / run

    def _ensure_permissions(self) -> bool:
        issues = platform_utils.check_permissions()
        if issues:
            dialogs.permission_issues(self, issues)
            self.append_log("Missing OS permission: " + ", ".join(i.name for i in issues), "error")
            return False
        return True

    def test_step(self) -> None:
        step = self.current_step()
        if step is None or self._call_thread is not None or self._run_thread is not None:
            return
        if not effective_template(step, self.active_preset):
            dialogs.info(self, tr("No target image"), tr("Select a target image for this step first."))
            return
        if not self._ensure_permissions():
            return
        self.save()
        self.append_log(f"Testing step '{step.name}' (no click) ...")
        self.showMinimized()

        preset = self.active_preset
        monitor = self._capture_monitor()

        def probe():
            events: list[Event] = []
            runner = Runner(self.profile, MssScreen(monitor), NullInput(), dry_run=True,
                            on_event=events.append, preset=preset)
            hit = runner.probe(step)
            diag = None if hit.found else runner.diagnose(step)  # why not? (same screen, restrictions lifted)
            return hit, events, diag

        def finish(result) -> None:
            hit, events, diag = result
            self._call_thread = None
            self._restore_window()
            for ev in events:
                if ev.kind == "preset" or ev.level == "warn" and "Display presets" in ev.message:
                    self.append_log(ev.message, ev.level)
            self._report_test(step, hit, diag)

        def failed(msg: str) -> None:
            self._call_thread = None
            self._restore_window()
            self.append_log(f"Test failed: {msg}", "error")

        def go():
            t = CallThread(probe)
            t.result.connect(finish)
            t.failed.connect(failed)
            self._call_thread = t
            t.start()

        QTimer.singleShot(500, go)

    def _report_test(self, step: Step, hit, diag) -> None:
        """Log the outcome of 'Test this step' in plain words: would a run click, and if not, why."""
        if hit.found:
            r = hit.rect
            self.append_log(f"Test '{step.name}': FOUND - confidence {hit.confidence:.3f} (threshold "
                            f"{step.threshold:.2f}). A run WOULD click at "
                            f"({r.center[0] + step.offset_x:.0f},{r.center[1] + step.offset_y:.0f}).")
            self.append_log(f"  match box at ({r.left:.0f},{r.top:.0f}) size {r.width:.0f}x{r.height:.0f}, "
                            f"scale {hit.scale:.2f}")
            self.highlighter.flash(r.as_tuple(), f"{hit.confidence:.2f} FOUND", True, 1500)
            return
        self.append_log(f"Test '{step.name}': NOT FOUND - confidence {hit.confidence:.3f} is below the threshold "
                        f"{step.threshold:.2f}. A run would NOT click this step.", "warn")
        if hit.rect.width:
            r = hit.rect
            self.append_log(f"  (the orange box marks only the BEST CANDIDATE at ({r.left:.0f},{r.top:.0f}), "
                            f"scale {hit.scale:.2f} - it is not a match)", "warn")
            self.highlighter.flash(r.as_tuple(), f"{hit.confidence:.2f} NOT FOUND - no click", False, 2500)
        if diag is not None and diag.kind != "ok":
            for line in diag.text.splitlines():
                self.append_log("  " + line, "warn")
            if self._ask_test_fix(step, diag) and diag.fix:
                self.apply_fix(step, diag.fix)

    def _ask_test_fix(self, step: Step, diag) -> bool:
        """Show the diagnosis; return True if the user wants the proposed fix applied."""
        return dialogs.diagnosis(self, step.name, diag.text, bool(diag.fix))

    def apply_fix(self, step: Step, fix: dict) -> None:
        """Apply a Diagnosis.fix to ``step`` (scale range / multi-scale / threshold / search box)."""
        done = []
        for key in ("multi_scale", "scale_min", "scale_max", "threshold", "ignore_background"):
            if key in fix:
                setattr(step, key, fix[key])
                done.append(f"{key}={fix[key]}")
        if "region" in fix:
            preset = self.active_preset
            mon = self._capture_monitor_info()
            ox, oy = (mon.left, mon.top) if (preset is not None and mon is not None) else (0, 0)
            l, t, w, h = fix["region"]
            set_region(step, preset, (int(l - ox), int(t - oy), int(w), int(h)))
            done.append("search box")
        step.normalise()
        self._show_step(step)
        self._refresh_items()
        self._mark_dirty()
        self.append_log(f"Applied fix to '{step.name}': {', '.join(done)}. Press 'Test this step' again.")

    def start_run(self, dry: bool) -> None:
        if self._run_thread is not None:
            return
        problems = self.profile.validate()
        if problems:
            dialogs.show_problems(self, problems)
            return
        try:
            parse_hotkey(self.profile.settings.stop_hotkey)
        except ValueError:
            pass
        if not self._ensure_permissions():
            return
        if not self.save():
            return
        try:
            screen = MssScreen(self._capture_monitor())
            inputs = PyAutoGuiInput()
        except Exception as exc:
            dialogs.error(self, tr("Cannot start"), f"Could not initialise screen/input: {exc}")
            return
        runner = Runner(self.profile, screen, inputs, dry_run=dry, preset=self.active_preset)
        thread = RunThread(runner)
        thread.event.connect(self._on_event)
        thread.done.connect(self._run_finished)
        self._run_thread = thread
        s = self.profile.settings
        self._stats = self._new_stats()
        self._stats.update(t0=time.monotonic(), dry=dry, total=len([x for x in self.profile.steps if x.enabled]))
        self._stats["countdown_until"] = self._stats["t0"] + s.start_delay
        self._set_running(True)
        self._refresh_dashboard()  # (the log drawer is not opened here: it only opens when the user clicks "Log")
        self._clock.start()
        self.stop_badge.show_badge(s.stop_hotkey)
        self.append_log(f"{'Dry run' if dry else 'Run'} starting - press {s.stop_hotkey or 'the Stop button'} "
                        f"or move the mouse to a screen corner to stop. Switch to your target window now "
                        f"({s.start_delay:.0f}s).", "warn")
        self._was_minimized_for_run = s.minimize_on_run
        if s.minimize_on_run:
            self.showMinimized()
        thread.start()

    def stop_run(self) -> None:
        if self._run_thread is not None:
            self._run_thread.request_stop()

    def _run_finished(self, result: RunResult) -> None:
        thread = self._run_thread
        self._run_thread = None
        if thread is not None:
            thread.wait(2000)
        self._clock.stop()
        self._set_running(False)
        self._restore_window()
        mode = "Dry run" if result.dry_run else "Run"
        self.append_log(f"{mode} finished: {result.reason} - {result.clicks} click(s), {result.loops} loop(s), "
                        f"{result.elapsed:.1f}s", "info", "finished")
        kind, text = {
            "completed": ("done", tr("Completed")), "user": ("sto", tr("Stopped")), "corner": ("sto", tr("Stopped (corner)")),
            "click_limit": ("sto", tr("Click limit reached")), "end_condition": ("sto", tr("End image seen")),
            "step_failed": ("err", tr("A step failed")), "idle": ("sto", tr("Stopped: nothing found")),
            "invalid_profile": ("err", tr("Invalid profile")), "error": ("err", tr("Error")),
        }.get(result.reason, ("sto", tr("Stopped")))
        self.pill.set_state(kind, text)
        self.stop_badge.show_result(kind, f"{text} \u00b7 {tr('{n} clicks', n=result.clicks)}")  # stays for 4 seconds
        self.step_list.set_current_step(None)

    def _start_or_stop(self) -> None:
        if self._running:
            self.stop_run()
        else:
            self.start_run(dry=False)

    def _set_running(self, running: bool) -> None:
        self._running = running
        has = bool(self.profile.steps)
        self.act_run.setEnabled(not running and has)
        self.act_dry.setEnabled(not running and has)
        self.act_stop.setEnabled(running)
        self.btn_dry.setEnabled(not running and has)
        self.btn_dry.setVisible(not running)
        self.btn_start.setEnabled(running or has)
        self.btn_start.setText(tr("Stop") + f" \u00b7 {self.profile.settings.stop_hotkey or 'F8'}" if running else tr("Start"))
        self.btn_start.setProperty("variant", "danger" if running else "primary")
        repolish(self.btn_start)
        set_icon(self.btn_start, "stop" if running else "play", "on_dg" if running else "on_ac", 18)
        for w in self._lockable_top:
            w.setEnabled(not running)
        self.btn_lang.setEnabled(not running)
        # the editors show a picture of each target: keep it off the screen while the matcher is looking
        self.step_editor.set_previews_visible(not running)
        self.end_editor.set_previews_visible(not running)
        for b in (self.btn_add, self.step_list):
            b.setEnabled(not running)
        self._refresh_items()
        if running:
            self.main_stack.setCurrentIndex(0)
            self.tabs.setVisible(False)
            self.right_stack.setCurrentIndex(2)
            self.dash_stop_hint.setText(tr("Press {key} to stop", key=self.profile.settings.stop_hotkey or "F8"))
        else:
            self.pill.set_state("idle", tr("Idle"))
            self._on_tab_changed(self.tabs.currentIndex())
            self._refresh_empty()
        self._refresh_dock_hint()

    # ================================================================== shutdown

    def closeEvent(self, event) -> None:  # noqa: N802
        if self._run_thread is not None:
            self._run_thread.request_stop()
            self._run_thread.wait(3000)
        if not self._confirm_discard():
            event.ignore()
            return
        self._clock.stop()
        self.hotkey.stop()
        self.key_watcher.stop()
        self.stop_badge.close()
        self.highlighter.close()
        event.accept()
