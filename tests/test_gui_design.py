"""The redesigned UI: simple / advanced modes, theme, language, step cards, hotkey capture, run dashboard."""
import pytest
from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtTest import QTest

from clickflow import storage
from clickflow.gui import i18n, icons, prefs, theme
from clickflow.gui.main_window import MainWindow
from clickflow.gui.widgets import ROLE_VIEW, HotkeyEdit, Segmented, StepDelegate, Switch, ThresholdSlider, hotkey_from_event
from clickflow.runner import Event


@pytest.fixture()
def win(qapp, tmp_path):
    w = MainWindow(storage.new_profile("design", root=tmp_path / "profiles"), start_hotkey=False)
    w.show()
    yield w
    w.dirty = False
    w.close()


def test_empty_profile_shows_the_welcome_screen_and_disables_start(win):
    assert win.main_stack.currentIndex() == 1 and not win.btn_start.isEnabled() and not win.btn_dry.isEnabled()
    win.add_step()
    assert win.main_stack.currentIndex() == 0 and win.btn_start.isEnabled() and win.step_count.text() == "1"
    win.step_list.setCurrentRow(0)
    win.delete_step()
    assert win.main_stack.currentIndex() == 1


def test_simple_mode_hides_the_advanced_groups_and_remembers_the_choice(win):
    win.add_step()
    e = win.step_editor
    assert not win.tabs.isVisibleTo(win) and not e.grp_match.isVisibleTo(e) and not e.grp_wait.isVisibleTo(e)
    assert e.click_type.isVisibleTo(e) and e.only_if_found.isVisibleTo(e) and win.run_card.isVisibleTo(win)
    win.set_mode(True)
    assert win.tabs.isVisibleTo(win) and e.click_type.isVisibleTo(e) and e.grp_click.isVisibleTo(e)  # tab 0: Step
    assert not e.grp_match.isVisibleTo(e) and not e.grp_wait.isVisibleTo(e)
    win.tabs.setCurrentIndex(1)  # Match & area
    assert e.grp_match.isVisibleTo(e) and e.grp_area.isVisibleTo(e) and not e.target_card.isVisibleTo(e)
    win.tabs.setCurrentIndex(2)  # Waits & fallbacks
    assert e.grp_wait.isVisibleTo(e) and not e.grp_match.isVisibleTo(e)
    assert prefs.get("mode") == "advanced"
    win.tabs.setCurrentIndex(3)  # Global
    assert win.right_stack.currentIndex() == 1 and win.run_card.parentWidget() is not None
    win.set_mode(False)
    assert win.right_stack.currentIndex() == 0 and prefs.get("mode") == "simple" and e.section() == 0


def test_the_run_card_is_one_widget_shared_by_both_modes(win):
    """Loop / speed / click-limit controls must keep working after moving between the simple and advanced layouts."""
    win.add_step()
    win.set_mode(True)
    win.s_loops.setValue(9)
    win.set_mode(False)
    assert win.profile.settings.loop_count == 9 and win.s_loops.value() == 9


def test_infinite_toggle_and_click_limit_switch(win):
    win.s_infinite.setChecked(True)
    assert win.profile.settings.infinite_loop and not win.s_loops.isEnabled() and win.repeat_seg.currentIndex() == 1
    win.s_infinite.setChecked(False)
    assert not win.profile.settings.infinite_loop and win.s_loops.isEnabled()
    win.max_row.switch.setChecked(False)
    assert win.profile.settings.max_total_clicks == 0
    win.max_row.switch.setChecked(True)
    assert win.profile.settings.max_total_clicks == 500  # back to the remembered value
    win.s_maxclicks.setValue(0)
    assert not win.max_row.switch.isChecked()


def test_speed_presets_move_the_slider_and_the_slider_selects_a_preset(win):
    win.speed_seg.setCurrentIndex(0)
    assert win.speed_slider.value() == 0 and win.profile.settings.speed == 0
    win.speed_seg.setCurrentIndex(2)
    assert win.speed_slider.value() == 100
    win.speed_slider.setValue(33)
    assert win.speed_seg.currentIndex() == -1  # between presets: nothing highlighted
    win.speed_slider.setValue(50)
    assert win.speed_seg.currentData() == 50


def test_step_cards_toggle_with_their_switch(qapp, win):
    win.add_step()
    qapp.processEvents()  # let the list lay out so the card has a real position
    item = win.step_list.item(0)
    rect = win.step_list.visualItemRect(item)
    card = StepDelegate.card_rect(rect)
    sw = StepDelegate.switch_rect(card)
    assert win.profile.steps[0].enabled
    QTest.mouseClick(win.step_list.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, sw.center())
    assert not win.profile.steps[0].enabled and item.checkState() == Qt.CheckState.Unchecked and win.dirty


def test_running_hides_thumbnails_in_the_list_and_swaps_the_start_button(win):
    win.add_step()
    win.step_list.setCurrentRow(0)
    assert win.btn_start.property("variant") == "primary"
    win._set_running(True)
    try:
        assert win.right_stack.currentIndex() == 2 and not win.tabs.isVisibleTo(win)
        assert win.btn_start.property("variant") == "danger" and "Stop" in win.btn_start.text()
        assert win.step_list.item(0).data(Qt.ItemDataRole.UserRole + 1)["hidden"] is True
        assert not win.step_editor.thumb.isVisibleTo(win.step_editor) and win.step_editor.thumb_hidden.isVisibleTo(win.step_editor)
        assert not win.step_list.isEnabled() and not win.mode_seg.isEnabled()
    finally:
        win._set_running(False)
    assert win.btn_start.property("variant") == "primary" and win.step_list.isEnabled()
    assert win.step_list.item(0).data(Qt.ItemDataRole.UserRole + 1)["hidden"] is False


def test_dashboard_counts_clicks_loops_and_the_current_step(win):
    win.add_step()
    win.add_step()
    win.profile.settings.max_total_clicks = 4
    win._set_running(True)
    try:
        win._stats.update(t0=__import__("time").monotonic(), total=2, started=True)
        name = win.profile.steps[1].name
        for ev in (Event("loop", "--- loop 1/inf ---", "info", 0.0, {"loop": 1}),
                   Event("step", f"Step '{name}'", "info", 0.0, {"step": name}),
                   Event("click", "left-click at (1,2)", "info", 0.0, {"x": 1, "y": 2, "dry": False}),
                   Event("click", "left-click at (1,2)", "info", 0.0, {"x": 1, "y": 2, "dry": False})):
            win._on_event(ev)
        assert win.tile_clicks.n.text() == "2" and win.tile_loops.n.text() == "1"
        assert win.tile_step.n.text() == "2 / 2" and win.dash_progress.value() == 500
    finally:
        win._set_running(False)


def test_there_is_only_a_light_theme_and_its_icons_render(qapp):
    theme.apply(qapp)
    assert theme.mode() == "light" and list(theme.TOKENS) == ["light"] and not hasattr(theme, "toggle")
    px = icons.pixmap("play", theme.tok("tx"), 16, 2.0)
    assert not px.isNull() and px.width() == 32
    assert icons.has("play") and not icons.has("no-such-icon")
    # the eight status shapes are separate 16 px symbols with their own colours
    for name in ("idle", "counting", "running", "stopped", "done", "fail", "warn", "info"):
        assert icons.has_status(name) and not icons.status_pixmap(name, 16, 2.0).isNull(), name


def test_design_tokens_match_the_hand_off(qapp):
    t = theme.TOKENS["light"]
    assert (t["bg"], t["tx"], t["ac"], t["ac_h"], t["ac_p"], t["on_ac"], t["sw_on"]) == (
        "#faf9f5", "#141413", "#cc785c", "#bb6a4f", "#a9583e", "#141413", "#141413")
    for key in ("on_ac_p", "fc_ring", "lg_bg", "lg_t"):  # the four keys added by the redesign
        assert key in t
    assert theme.qcolor("fc_ring").alpha() == round(0.15 * 255)  # rgba() tokens parse
    assert theme.overlay("ov_stop").name() == "#c64545" and theme.overlay("ov_dim").alpha() == round(0.6 * 255)


def test_language_switch_rebuilds_the_window_without_losing_state(win):
    win.add_step()
    win.step_list.setCurrentRow(0)
    win.s_loops.setValue(7)
    win.append_log("hello log")
    win.set_mode(True)
    win._toggle_language()
    try:
        assert i18n.language() == "zh"
        assert win.btn_start.text() == "开始" and win.profile.settings.loop_count == 7
        assert win.s_loops.value() == 7 and win.step_list.count() == 1 and win.step_list.currentRow() == 0
        assert win.dirty  # the unsaved edits are still unsaved
        assert "hello log" in win.log_view.toPlainText()
        assert win._advanced and win.step_editor.is_advanced()
    finally:
        win._toggle_language()
    assert i18n.language() == "en" and win.btn_start.text() == "Start"


def test_chinese_run_log_lines():
    from clickflow.gui.i18n_zh import format_event

    assert format_event(Event("loop", "--- loop 3/inf ---", "info", 0, {})) == "第 3 轮开始"
    assert "置信度 0.94" in format_event(Event("match", "'Like': FOUND, confidence 0.941 (threshold 0.85)", "info", 0, {}))
    assert format_event(Event("click", "left-click at (812,604)", "info", 0, {})) == "左键点击 (812, 604)"
    assert format_event(Event("log", "something else", "info", 0, {})) == "something else"  # unknown lines pass through


def test_every_translated_string_keeps_its_placeholders():
    import re

    from clickflow.gui.i18n_zh import ZH

    ph = re.compile(r"\{[^}]*\}")
    for en, zh in ZH.items():
        assert sorted(ph.findall(en)) == sorted(ph.findall(zh)), (en, zh)


def test_hotkey_field_captures_a_key_press_instead_of_text(qapp):
    edit = HotkeyEdit()
    edit.show()
    got = []
    edit.editingFinished.connect(lambda: got.append(edit.text()))
    edit.setFocus()
    QTest.keyClick(edit, Qt.Key.Key_F9)
    QTest.keyClick(edit, Qt.Key.Key_S, Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier)
    QTest.keyClick(edit, Qt.Key.Key_Shift)  # a lone modifier is not a hotkey
    assert got == ["F9", "ctrl+shift+s"] and edit.text() == "ctrl+shift+s"
    from clickflow.hotkey import parse_hotkey

    assert parse_hotkey(edit.text()) == "<ctrl>+<shift>+s"  # what the field produces is always accepted


def test_invalid_hotkey_text_is_rejected_and_reverted(win, monkeypatch):
    from clickflow.gui import dialogs

    shown = []
    monkeypatch.setattr(dialogs, "warn", lambda parent, title, text: shown.append(title))
    win.s_hotkey.setText("not-a-key")
    win._apply_hotkey()
    assert shown == ["Invalid hotkey"] and win.s_hotkey.text() == win.profile.settings.stop_hotkey


def test_segmented_and_threshold_have_the_combo_and_float_apis(qapp):
    seg = Segmented()
    for text, data in (("a", 1), ("b", 2), ("c", 3)):
        seg.addItem(text, data)
    changes = []
    seg.currentIndexChanged.connect(changes.append)
    seg.setCurrentIndex(2)
    assert seg.currentData() == 3 and seg.findData(2) == 1 and changes == [2]
    seg.clearSelection()
    assert seg.currentIndex() == -1
    t = ThresholdSlider("x")
    got = []
    t.valueChanged.connect(got.append)
    t.setValue(0.93)
    assert abs(t.value() - 0.93) < 1e-9 and got and abs(got[-1] - 0.93) < 1e-9
    t.setValue(2.0)
    assert t.value() == 1.0  # clamped to the slider range
    s = Switch()
    s.setChecked(True)
    assert s.isChecked()


def test_hotkey_names_map_to_what_the_parser_accepts(qapp):
    from PySide6.QtGui import QKeyEvent
    from PySide6.QtCore import QEvent

    from clickflow.hotkey import parse_hotkey

    for key in (Qt.Key.Key_F1, Qt.Key.Key_F24, Qt.Key.Key_Space, Qt.Key.Key_Home, Qt.Key.Key_PageDown, Qt.Key.Key_A,
                Qt.Key.Key_5, Qt.Key.Key_Pause):
        ev = QKeyEvent(QEvent.Type.KeyPress, key, Qt.KeyboardModifier.NoModifier)
        name = hotkey_from_event(ev)
        assert name and parse_hotkey(name), (key, name)


def test_preset_button_sits_in_the_top_bar_and_reflects_the_active_preset(win, monkeypatch):
    from clickflow.screen import MonitorInfo

    mon = MonitorInfo(1, 0, 0, 2560, 1440, True)
    monkeypatch.setattr(MainWindow, "_detect_monitors", lambda self: [mon])
    assert win.preset_btn.isVisibleTo(win) and win.preset_btn.menu() is win.preset_menu  # always one click away, top-left
    assert win.preset_btn.text() == "Default" and "Default" in win.preset_menu.title()
    p = win.create_preset("Desk", mon)
    assert win.preset_btn.text() == "Desk" and "Desk" in win.preset_menu.title() and win.active_preset is p
    win._fill_more_menu()  # the preset no longer hides inside the "more" menu
    assert not any("Desk" in a.text() or "preset" in a.text().lower() for a in win.more_menu.actions())
    win._fill_preset_menu()
    acts = {a.text(): a for a in win.preset_menu.actions() if a.text()}
    checked = [t for t, a in acts.items() if a.isCheckable() and a.isChecked() and "Desk" in t]
    assert checked, list(acts)
    acts["Default (no preset)"].trigger()  # pick "no preset"
    assert win.active_preset is None and "Default" in win.preset_menu.title() and win.preset_btn.text() == "Default"
    win._fill_preset_menu()
    acts = {a.text(): a for a in win.preset_menu.actions() if a.text()}
    assert not acts["Rename preset..."].isEnabled() and not acts["Delete preset..."].isEnabled()  # nothing to rename
    win._set_running(True)
    try:
        assert not win.btn_more.isEnabled() and not win.profile_btn.isEnabled()  # locked while a run is active
        assert not win.preset_btn.isEnabled()
    finally:
        win._set_running(False)


def test_row_switch_updates_the_left_row_not_only_the_editor(win):
    win.add_step()
    win.add_step()
    win.step_list.setCurrentRow(0)
    item = win.step_list.item(0)
    assert item.data(ROLE_VIEW)["enabled"] is True
    item.setCheckState(Qt.CheckState.Unchecked)  # what a click on the row's switch does
    assert win.profile.steps[0].enabled is False
    assert item.data(ROLE_VIEW)["enabled"] is False  # the delegate paints from this, so the row's switch now shows "off"
    item.setCheckState(Qt.CheckState.Checked)
    assert win.profile.steps[0].enabled is True and item.data(ROLE_VIEW)["enabled"] is True


def test_starting_a_run_does_not_open_the_log_drawer(win, monkeypatch):
    assert not win.log_drawer.isVisibleTo(win)
    monkeypatch.setattr(MainWindow, "_ensure_permissions", lambda self: True)
    monkeypatch.setattr(MainWindow, "save", lambda self, *a, **k: True)
    monkeypatch.setattr("clickflow.gui.main_window.RunThread.start", lambda self: None)
    monkeypatch.setattr("clickflow.gui.main_window.MssScreen", lambda *a, **k: object())
    monkeypatch.setattr("clickflow.gui.main_window.PyAutoGuiInput", lambda *a, **k: object())
    win.add_step()
    monkeypatch.setattr(win.profile, "validate", lambda: [])
    try:
        win.start_run(True)
        assert win._running
        assert not win.log_drawer.isVisibleTo(win) and not win.log_toggle.isChecked()  # only the user's click on "Log" opens it
        win.log_toggle.setChecked(True)
        assert win.log_drawer.isVisibleTo(win)
    finally:
        win._run_thread = None
        win._clock.stop()
        win.stop_badge.close()
        win._set_running(False)


def test_backgrounds_are_warm_neutrals_without_a_blue_cast(qapp):
    for key in ("bg", "pn", "cd"):
        c = theme.TOKENS["light"][key].lstrip("#")
        r, g, b = (int(c[i:i + 2], 16) for i in (0, 2, 4))
        assert b <= r and abs(b - r) <= 12, (key, c)  # cream: never bluer than red


def test_english_is_the_default_language(monkeypatch, qapp):
    monkeypatch.delenv("CLICKFLOW_LANG", raising=False)
    prefs.reset_cache()
    assert i18n.default_language() == "en"  # no more guessing from the system locale
    prefs.set("lang", "zh")
    assert i18n.default_language() == "zh"
    prefs.set("lang", "")


def test_add_step_button_selects_a_target_at_once_and_esc_removes_the_empty_step(win, monkeypatch):
    started = []
    monkeypatch.setattr(MainWindow, "capture_step_template", lambda self: started.append(self.profile.steps[-1].id))
    win.add_step_and_select()
    assert len(win.profile.steps) == 1 and started == [win.profile.steps[0].id]
    win._capture_cancelled()  # Esc on the first drag: the step has no target, so it goes away again
    assert win.profile.steps == [] and win.main_stack.currentIndex() == 1
    win.add_step_and_select()
    win.profile.steps[-1].template = "templates/x.png"  # a target was captured, then the second drag (area) is skipped
    win._capture_cancelled()
    assert len(win.profile.steps) == 1


def test_run_rows_fit_without_scrolling_at_both_window_sizes(win):
    win.add_step()
    win.step_list.setCurrentRow(0)
    for size in ((1180, 820), (960, 640)):
        win.resize(*size)
        QTest.qWait(50)
        assert win.scroll_step.verticalScrollBar().maximum() == 0, size  # the three beginner steps never need a scroll bar


def test_stop_badge_shows_live_status_then_a_result(qapp):
    from clickflow.gui.overlays import StopBadge

    b = StopBadge()
    try:
        b.show_badge("F8")
        w0 = b.width()
        b._last_update = 0.0
        b.set_status("Running \u00b7 37 clicks")
        assert "37 clicks" in b.text() and "F8" in b.text() and b.width() > w0 and b.height() == 44
        b.set_status("Running \u00b7 38 clicks")  # throttled: at most two refreshes a second
        assert "37 clicks" in b.text()
        got = []
        b.clicked.connect(lambda: got.append(1))
        QTest.mouseClick(b, Qt.MouseButton.LeftButton)
        assert got == [1]  # a click on the running badge asks for a stop
        b.show_result("done", "Completed \u00b7 40 clicks")
        assert "DONE" in b.text() and b.isVisible()
        QTest.mouseClick(b, Qt.MouseButton.LeftButton)
        assert got == [1] and not b.isVisible()  # on the result badge a click only dismisses it
    finally:
        b.close()


def test_step_rows_are_60_px_and_show_only_warnings(qapp):
    from clickflow.gui.widgets import ROW_H, StepDelegate

    assert ROW_H == 60
    card = StepDelegate.card_rect(QRect(0, 100, 288, 60))
    assert card.height() == 60 and StepDelegate.switch_rect(card).width() == 36


def test_albert_sans_is_bundled_and_nothing_is_bold(qapp):
    from pathlib import Path

    from PySide6.QtGui import QFont

    families = theme.load_fonts()
    assert "Albert Sans" in families  # assets/fonts is found and registered
    fonts = Path(theme.__file__).resolve().parents[2] / "assets" / "fonts"
    assert (fonts / "OFL-AlbertSans.txt").is_file() and not list(fonts.glob("*nthropic*"))  # its licence ships with it
    qss = theme.build_qss(theme.TOKENS["light"], theme._assets(theme.TOKENS["light"], "light"))
    assert '"Albert Sans"' in qss and "Anthropic" not in qss
    for heavy in ("font-weight: 600", "font-weight: 700", "font-weight: bold"):
        assert heavy not in qss, heavy  # the heaviest weight is Medium (500); Chinese is Regular
    assert theme.semibold() == QFont.Weight.Medium
    i18n.set_language("zh", save=False)
    try:
        assert theme.semibold() == QFont.Weight.Normal and "font-weight: 500" not in theme.build_qss(
            theme.TOKENS["light"], theme._assets(theme.TOKENS["light"], "light"))
    finally:
        i18n.set_language("en", save=False)


def test_switch_slides_instead_of_jumping_and_snaps_for_code_changes(qapp):
    sw = Switch(False)
    sw.show()
    try:
        assert sw._t == 0.0
        sw.setChecked(True)  # a change made from code jumps straight there
        assert sw._t == 1.0
        sw.setChecked(False)
        QTest.mouseClick(sw, Qt.MouseButton.LeftButton)  # a click slides
        assert sw.isChecked() and sw._t < 1.0
        seen = set()
        for _ in range(40):
            QTest.qWait(10)
            seen.add(round(sw._t, 1))
        assert sw._t == 1.0 and any(0.0 < v < 1.0 for v in seen)  # it passed through in-between positions
    finally:
        sw.close()


def test_quick_repeated_clicks_on_a_switch_are_all_counted(qapp):
    sw = Switch(False)
    sw.show()
    flips = []
    sw.toggled.connect(flips.append)
    try:
        for _ in range(7):  # faster than the slide takes
            QTest.mouseClick(sw, Qt.MouseButton.LeftButton)
            QTest.qWait(15)
        assert len(flips) == 7 and sw.isChecked()
        QTest.qWait(300)
        assert sw._t == 1.0  # the thumb ends where the state says
    finally:
        sw.close()


def test_row_switch_animates_and_every_click_counts_even_double_clicks(win):
    win.add_step()
    win.add_step()
    win.step_list.setCurrentRow(1)
    lst, d = win.step_list, win.step_list.delegate
    item = lst.item(0)
    key = item.data(Qt.ItemDataRole.UserRole)
    lst.viewport().repaint()
    assert d._switch_pos(key, True) == 1.0
    item.setCheckState(Qt.CheckState.Unchecked)
    positions = []
    for _ in range(25):
        QTest.qWait(12)
        lst.viewport().repaint()
        positions.append(d._sw[key][0])
    assert any(0.0 < v < 1.0 for v in positions) and positions[-1] == 0.0  # slid from on to off, gradually
    # a quick second click reaches the delegate as a double-click event: it must flip the switch too
    pos = StepDelegate.switch_rect(StepDelegate.card_rect(lst.visualItemRect(item))).center()
    QTest.mouseDClick(lst.viewport(), Qt.MouseButton.LeftButton, pos=pos)
    assert win.profile.steps[0].enabled is True


def test_language_switch_shows_no_stray_windows_and_keeps_the_saved_state(win):
    from PySide6.QtCore import QEvent, QObject
    from PySide6.QtWidgets import QApplication, QWidget

    class Spy(QObject):
        def __init__(self):
            super().__init__()
            self.shown = []

        def eventFilter(self, obj, ev):  # noqa: N802
            if ev.type() == QEvent.Type.Show and isinstance(obj, QWidget) and obj.isWindow() and obj is not win:
                self.shown.append(type(obj).__name__)
            return False

    win.add_step()
    win.step_list.setCurrentRow(0)
    win.dirty = False
    win._refresh_title()
    spy = Spy()
    app = QApplication.instance()
    app.installEventFilter(spy)
    try:
        for _ in range(2):  # en -> zh -> en
            win._toggle_language()
            QTest.qWait(20)
    finally:
        app.removeEventFilter(spy)
    assert spy.shown == []  # a widget shown before it has a parent would flash up as a small window
    assert not win.dirty and "*" not in win.windowTitle()  # switching language is not an edit
    win.step_editor.name.setText("Renamed")
    win._toggle_language()  # a value typed but not yet committed still counts as a change
    assert win.profile.steps[0].name == "Renamed" and win.dirty
    win._toggle_language()


def test_restyling_is_skipped_when_nothing_changed(qapp, monkeypatch):
    theme.apply(qapp)
    calls = []
    real = qapp.setStyleSheet
    monkeypatch.setattr(type(qapp), "setStyleSheet", lambda self, s: (calls.append(1), real(s))[1], raising=False)
    theme.apply(qapp)  # same language: identical stylesheet, so nothing is set again
    assert calls == []
    assert theme.load_fonts() == theme.load_fonts() and "Albert Sans" in theme.load_fonts()  # registered once, then cached
    theme.clear_stylesheet(qapp)
    theme.apply(qapp)  # after clearing it is applied again
    assert calls and qapp.styleSheet()


def test_font_stack_is_short_in_english_and_adds_a_cjk_font_in_chinese(qapp):
    i18n.set_language("en", save=False)
    assert theme.ui_families() == ["Albert Sans"]
    i18n.set_language("zh", save=False)
    try:
        fams = theme.ui_families()
        assert fams[0] == "Albert Sans" and len(fams) == 2
    finally:
        i18n.set_language("en", save=False)
