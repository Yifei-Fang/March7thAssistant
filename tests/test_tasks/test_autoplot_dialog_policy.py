"""A1 对话层：只跳过、或点单列第一项，其余界面停止并写明类型。"""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tasks.tool.autoplot._config import Crop, Img
from tasks.tool.autoplot._dialog_policy import (
    COLUMN_X_TOLERANCE,
    CHOICE_KIND_EXIT,
    CHOICE_KIND_MESSAGE,
    CHOICE_KIND_PREV_PAGE,
    CHOICE_KIND_SELECT,
    ChoiceMark,
    DialogAction,
    DialogSignals,
    DialogUiType,
    decide_dialog_action,
    mark_from_match,
)


def decide(signals, *, auto_skip=True, auto_click=True):
    return decide_dialog_action(signals, auto_skip=auto_skip, auto_click=auto_click)


def mark(kind, x, y, box=((1, 1), (2, 2))):
    return ChoiceMark(kind, x, y, box)


def test_skip_beats_single_column_choices():
    choice = mark(CHOICE_KIND_SELECT, 0.70, 0.50)
    decision = decide(DialogSignals(
        skip_visible=True,
        continue_visible=True,
        dialog_chrome=True,
        marks=(choice,),
    ))
    assert decision.action is DialogAction.CLICK_SKIP
    assert decision.ui_type is DialogUiType.SKIPPABLE_DIALOG
    assert decision.first_option is None


def test_skip_is_ignored_when_auto_skip_is_off():
    decision = decide(
        DialogSignals(skip_visible=True, continue_visible=True, dialog_chrome=True),
        auto_skip=False,
    )
    assert decision.action is DialogAction.CLICK_CONTINUE
    assert decision.ui_type is DialogUiType.STANDING_DIALOG


def test_single_column_clicks_the_upper_option():
    lower = mark(CHOICE_KIND_SELECT, 0.70, 0.62, box=((10, 60), (20, 70)))
    upper = mark(CHOICE_KIND_SELECT, 0.71, 0.40, box=((10, 20), (20, 30)))
    decision = decide(DialogSignals(marks=(lower, upper), continue_visible=True))
    assert decision.action is DialogAction.CLICK_FIRST_OPTION
    assert decision.ui_type is DialogUiType.SINGLE_COLUMN_CHOICE
    assert decision.first_option is upper


def test_duplicate_template_hits_collapse_to_one_option():
    first = mark(CHOICE_KIND_SELECT, 0.700, 0.400, box=((1, 1), (2, 2)))
    duplicate = mark(CHOICE_KIND_SELECT, 0.708, 0.406, box=((3, 3), (4, 4)))
    decision = decide(DialogSignals(marks=(duplicate, first)))
    assert decision.action is DialogAction.CLICK_FIRST_OPTION
    assert decision.first_option is first


def test_column_edge_stays_single_until_tolerance_is_exceeded():
    left = mark(CHOICE_KIND_SELECT, 0.70, 0.40)
    still_one = mark(CHOICE_KIND_SELECT, 0.70 + COLUMN_X_TOLERANCE - 0.001, 0.55)
    second_column = mark(CHOICE_KIND_SELECT, 0.70 + COLUMN_X_TOLERANCE + 0.01, 0.55)
    assert decide(DialogSignals(marks=(left, still_one))).action is DialogAction.CLICK_FIRST_OPTION
    multi = decide(DialogSignals(marks=(left, second_column)))
    assert multi.action is DialogAction.STOP
    assert multi.ui_type is DialogUiType.MULTI_COLUMN_CHOICE
    assert multi.first_option is None


def test_choice_screen_does_not_fall_through_to_continue():
    decision = decide(DialogSignals(
        continue_visible=True,
        marks=(
            mark(CHOICE_KIND_SELECT, 0.40, 0.40),
            mark(CHOICE_KIND_SELECT, 0.70, 0.40),
        ),
    ))
    assert decision.action is DialogAction.STOP
    assert decision.ui_type is DialogUiType.MULTI_COLUMN_CHOICE


@pytest.mark.parametrize("kind", [CHOICE_KIND_EXIT, CHOICE_KIND_PREV_PAGE, "unknown"])
def test_nonstandard_marks_stop(kind):
    decision = decide(DialogSignals(marks=(
        mark(CHOICE_KIND_SELECT, 0.70, 0.40),
        mark(kind, 0.70, 0.55),
    )))
    assert decision.action is DialogAction.STOP
    assert decision.ui_type is DialogUiType.NONSTANDARD_CHOICE


def test_message_mark_is_phone_not_a_choice():
    decision = decide(DialogSignals(marks=(mark(CHOICE_KIND_MESSAGE, 0.70, 0.40),)))
    assert decision.action is DialogAction.STOP
    assert decision.ui_type is DialogUiType.PHONE_SMS


def test_auto_click_off_waits_on_a_single_column():
    decision = decide(
        DialogSignals(marks=(mark(CHOICE_KIND_SELECT, 0.70, 0.40),)),
        auto_click=False,
    )
    assert decision.action is DialogAction.WAIT
    assert decision.ui_type is DialogUiType.SINGLE_COLUMN_CHOICE
    assert decision.first_option is None


def test_auto_click_off_still_stops_on_multiple_columns():
    decision = decide(
        DialogSignals(marks=(
            mark(CHOICE_KIND_SELECT, 0.30, 0.40),
            mark(CHOICE_KIND_SELECT, 0.70, 0.40),
        )),
        auto_click=False,
    )
    assert decision.action is DialogAction.STOP
    assert decision.ui_type is DialogUiType.MULTI_COLUMN_CHOICE


def test_non_finite_mark_stops():
    decision = decide(DialogSignals(marks=(mark(CHOICE_KIND_SELECT, float("nan"), 0.4),)))
    assert decision.action is DialogAction.STOP
    assert decision.ui_type is DialogUiType.NONSTANDARD_CHOICE


def test_standing_dialog_without_skip_clicks_continue():
    decision = decide(DialogSignals(continue_visible=True, dialog_chrome=True))
    assert decision.action is DialogAction.CLICK_CONTINUE
    assert decision.ui_type is DialogUiType.STANDING_DIALOG


def test_dialog_chrome_without_continue_waits_for_text():
    decision = decide(DialogSignals(dialog_chrome=True))
    assert decision.action is DialogAction.WAIT
    assert decision.ui_type is DialogUiType.DIALOG_TEXT_PENDING


def test_empty_screen_is_a_cutscene_wait():
    decision = decide(DialogSignals())
    assert decision.action is DialogAction.WAIT
    assert decision.ui_type is DialogUiType.CUTSCENE
    assert decision.first_option is None


@pytest.mark.parametrize(
    ("field", "ui_type"),
    [
        ("investigate_visible", DialogUiType.INVESTIGATE),
        ("party_setup_visible", DialogUiType.PARTY_SETUP),
        ("challenge_start_visible", DialogUiType.CHALLENGE_START),
        ("combat_visible", DialogUiType.COMBAT_ENTRY),
        ("teleport_visible", DialogUiType.TELEPORT),
        ("phone_visible", DialogUiType.PHONE_SMS),
        ("confirm_visible", DialogUiType.CONFIRM_CANCEL),
        ("system_ui_visible", DialogUiType.SYSTEM_UI),
        ("unknown_button_visible", DialogUiType.UNKNOWN_BUTTON),
    ],
)
def test_blocked_screens_stop_without_a_first_option(field, ui_type):
    decision = decide(DialogSignals(
        skip_visible=True,
        continue_visible=True,
        dialog_chrome=True,
        marks=(mark(CHOICE_KIND_SELECT, 0.70, 0.40),),
        **{field: True},
    ))
    assert decision.action is DialogAction.STOP
    assert decision.ui_type is ui_type
    assert decision.first_option is None


def test_mark_from_match_uses_crop_fraction_and_absolute_box():
    choice = mark_from_match(
        CHOICE_KIND_SELECT,
        Crop.CHOICE_BAND,
        (800, 360),
        (0, 0),
        (20, 20),
        (100, 80, 800, 360),
        1,
    )
    crop_x, crop_y, crop_w, crop_h = Crop.CHOICE_BAND
    assert choice.x == pytest.approx(crop_x + (10 / 800) * crop_w)
    assert choice.y == pytest.approx(crop_y + (10 / 360) * crop_h)
    assert choice.box == ((100, 80), (120, 100))


def test_mark_from_match_without_window_position_has_no_click_box():
    choice = mark_from_match(
        CHOICE_KIND_SELECT,
        Crop.CHOICE_BAND,
        (800, 360),
        (0, 0),
        (20, 20),
        None,
        1,
    )
    assert choice.box == ()


def test_mark_from_match_rejects_empty_screenshot():
    with pytest.raises(RuntimeError):
        mark_from_match(CHOICE_KIND_SELECT, Crop.CHOICE_BAND, (0, 360), (0, 0), (1, 1), (0, 0), 1)


def test_observe_dialog_collects_signals_without_clicking(monkeypatch):
    import tasks.tool.autoplot._detector as detector

    auto = Mock()
    auto.screenshot = SimpleNamespace(size=(800, 360))
    auto.screenshot_pos = (100, 80, 800, 360)
    auto.screenshot_scale_factor = 2
    auto.take_screenshot.return_value = True
    auto.is_rgb_ratio_above_threshold.return_value = False
    auto.click_element.side_effect = AssertionError("观察对话时不能点击")
    auto.click_element_with_pos.side_effect = AssertionError("观察对话时不能点击")

    def find_element(target, find_type, *args, **kwargs):
        if find_type == "image_with_multiple_targets" and target == Img.SELECT:
            return [((0, 0), (20, 20)), ((400, 100), (420, 120))]
        if find_type == "image" and target == Img.CONFIRM:
            return ((1, 1), (8, 8))
        if find_type == "yolo":
            return ((4, 4), (12, 12))
        return None

    auto.find_element.side_effect = find_element
    monkeypatch.setattr(detector, "auto", auto)

    signals = detector.SceneDetector.observe_dialog()

    assert auto.take_screenshot.call_args_list[1].kwargs["crop"] == Crop.CHOICE_BAND
    assert signals.confirm_visible is True
    assert signals.skip_visible is True
    assert len(signals.marks) == 2
    assert signals.marks[0].box == ((100, 80), (110, 90))
    assert {mark.kind for mark in signals.marks} == {CHOICE_KIND_SELECT}
    auto.click_element.assert_not_called()
    auto.click_element_with_pos.assert_not_called()

    # 两列选项加上确认框时，确认框优先，不能去点第一项。
    decision = decide(signals)
    assert decision.action is DialogAction.STOP
    assert decision.ui_type is DialogUiType.CONFIRM_CANCEL


def test_click_choice_refuses_exit_and_missing_box(monkeypatch):
    import tasks.tool.autoplot._detector as detector

    auto = Mock()
    monkeypatch.setattr(detector, "auto", auto)
    assert detector.SceneDetector.click_choice(mark(CHOICE_KIND_EXIT, 0.7, 0.4)) is False
    assert detector.SceneDetector.click_choice(
        ChoiceMark(CHOICE_KIND_SELECT, 0.7, 0.4, box=())
    ) is False
    auto.click_element_with_pos.assert_not_called()
    assert detector.SceneDetector.click_choice(
        mark(CHOICE_KIND_SELECT, 0.7, 0.4, box=((8, 9), (12, 14)))
    ) is True
    auto.click_element_with_pos.assert_called_once_with(((8, 9), (12, 14)))


def _engine_plot(qapp, monkeypatch):
    import tasks.tool.autoplot._engine as module

    foreground = Mock(return_value=SimpleNamespace(title="test-game"))
    monkeypatch.setattr(module, "gw", SimpleNamespace(getActiveWindow=foreground))
    detector = Mock()
    detector.is_dialog_scene.return_value = True
    detector.detect_skip_button.return_value = False
    detector.click_confirm.return_value = True
    detector.is_auto_battle_off.return_value = False
    detector.is_click_blocked_screen.return_value = False
    detector.observe_dialog.return_value = DialogSignals(continue_visible=True, dialog_chrome=True)
    monkeypatch.setattr(module, "SceneDetector", detector)
    automation = Mock()
    automation.click_element.return_value = False
    monkeypatch.setattr(module, "auto", automation)
    monkeypatch.setattr(module, "log", Mock())
    pending = []
    monkeypatch.setattr(module.QTimer, "singleShot", lambda delay, callback: pending.append(callback))
    engine = module.AutoPlot("test-game")
    engine.start()
    plot = SimpleNamespace(
        module=module, engine=engine, detector=detector,
        automation=automation, pending=pending,
    )
    return plot


def _run_dialog(plot):
    plot.engine._monitor_loop()
    plot.pending.pop(0)()


def test_confirm_dialog_stops_and_logs_once(qapp, monkeypatch):
    plot = _engine_plot(qapp, monkeypatch)
    try:
        plot.detector.observe_dialog.return_value = DialogSignals(
            confirm_visible=True,
            skip_visible=True,
            marks=(mark(CHOICE_KIND_SELECT, 0.7, 0.4),),
        )
        _run_dialog(plot)
        assert plot.engine._state == plot.module.EngineState.HOLDING
        plot.detector.detect_skip_button.assert_not_called()
        plot.detector.click_choice.assert_not_called()
        plot.automation.click_element.assert_not_called()
        warning = plot.module.log.warning.call_args.args[0]
        assert "ui_type=confirm_cancel" in warning
        assert "action=stop" in warning

        plot.pending.pop(0)()
        assert plot.module.log.warning.call_count == 1

        plot.detector.observe_dialog.return_value = DialogSignals(
            continue_visible=True, dialog_chrome=True,
        )
        plot.pending.pop(0)()
        plot.automation.click_element.assert_called_once()
        assert plot.engine._state == plot.module.EngineState.DIALOG_ACTIVE
        assert "ui_type=standing_dialog" in plot.module.log.info.call_args.args[0]
        assert "action=click_continue" in plot.module.log.info.call_args.args[0]
    finally:
        plot.engine.stop()


def test_first_option_clicks_only_the_upper_mark(qapp, monkeypatch):
    plot = _engine_plot(qapp, monkeypatch)
    try:
        lower = mark(CHOICE_KIND_SELECT, 0.70, 0.70, box=((1, 80), (4, 90)))
        upper = mark(CHOICE_KIND_SELECT, 0.70, 0.40, box=((1, 20), (4, 30)))
        plot.detector.observe_dialog.return_value = DialogSignals(marks=(lower, upper))
        _run_dialog(plot)
        plot.detector.click_choice.assert_called_once_with(upper)
        plot.automation.click_element.assert_not_called()
        plot.detector.detect_skip_button.assert_not_called()
    finally:
        plot.engine.stop()


def test_missing_choice_box_stops_instead_of_inventing_a_click(qapp, monkeypatch):
    plot = _engine_plot(qapp, monkeypatch)
    try:
        plot.detector.observe_dialog.return_value = DialogSignals(
            marks=(ChoiceMark(CHOICE_KIND_SELECT, 0.70, 0.40, box=()),),
        )
        _run_dialog(plot)
        plot.detector.click_choice.assert_not_called()
        assert plot.engine._state == plot.module.EngineState.HOLDING
        assert "ui_type=nonstandard_choice" in plot.module.log.warning.call_args.args[0]
    finally:
        plot.engine.stop()


def test_cutscene_waits_without_clicking(qapp, monkeypatch):
    plot = _engine_plot(qapp, monkeypatch)
    try:
        plot.detector.observe_dialog.return_value = DialogSignals()
        _run_dialog(plot)
        plot.automation.click_element.assert_not_called()
        plot.detector.click_choice.assert_not_called()
        warning = plot.module.log.warning.call_args.args[0]
        assert "ui_type=cutscene" in warning
        assert "action=wait" in warning
    finally:
        plot.engine.stop()


def test_leaving_dialog_resumes_explore(qapp, monkeypatch):
    plot = _engine_plot(qapp, monkeypatch)
    try:
        plot.detector.observe_dialog.return_value = DialogSignals(combat_visible=True)
        _run_dialog(plot)
        assert plot.engine._state == plot.module.EngineState.HOLDING
        plot.detector.is_dialog_scene.return_value = False
        plot.engine._monitor_loop()
        assert plot.engine._state == plot.module.EngineState.MONITORING
        assert "ui_type=explore" in plot.module.log.info.call_args.args[0]
        assert "action=resume" in plot.module.log.info.call_args.args[0]
        stale = plot.pending.pop(0)
        stale()
        plot.detector.click_choice.assert_not_called()
        plot.automation.click_element.assert_not_called()
    finally:
        plot.engine.stop()


def test_skip_clicks_skip_then_its_confirm_only(qapp, monkeypatch):
    plot = _engine_plot(qapp, monkeypatch)
    try:
        plot.detector.observe_dialog.return_value = DialogSignals(
            skip_visible=True,
            continue_visible=True,
            marks=(mark(CHOICE_KIND_SELECT, 0.7, 0.4),),
        )
        plot.detector.detect_skip_button.return_value = True
        _run_dialog(plot)
        plot.detector.detect_skip_button.assert_called_once()
        plot.detector.click_confirm.assert_called_once()
        plot.detector.click_choice.assert_not_called()
        plot.automation.click_element.assert_not_called()
    finally:
        plot.engine.stop()


def test_disabled_auto_click_waits_for_the_user(qapp, monkeypatch):
    plot = _engine_plot(qapp, monkeypatch)
    try:
        plot.engine.auto_click = False
        plot.detector.observe_dialog.return_value = DialogSignals(
            marks=(mark(CHOICE_KIND_SELECT, 0.7, 0.4),),
        )
        _run_dialog(plot)
        assert plot.engine._state == plot.module.EngineState.WAITING_OPTION
        plot.detector.click_choice.assert_not_called()
        plot.automation.click_element.assert_not_called()
    finally:
        plot.engine.stop()


def test_combat_screen_presses_auto_battle_and_clicks_nothing_else(qapp, monkeypatch):
    plot = _engine_plot(qapp, monkeypatch)
    try:
        plot.detector.is_dialog_scene.return_value = False
        plot.detector.is_auto_battle_off.return_value = True
        plot.engine._monitor_loop()
        plot.automation.press_key.assert_called_once()
        plot.detector.handle_phone_dialogs.assert_not_called()
        plot.detector.click_close.assert_not_called()
        plot.detector.is_click_blocked_screen.assert_not_called()
    finally:
        plot.engine.stop()


def test_blocked_explore_screen_does_not_click_phone_or_close(qapp, monkeypatch):
    plot = _engine_plot(qapp, monkeypatch)
    try:
        plot.detector.is_dialog_scene.return_value = False
        plot.detector.is_click_blocked_screen.return_value = True
        plot.engine._monitor_loop()
        plot.detector.handle_phone_dialogs.assert_not_called()
        plot.detector.click_close.assert_not_called()
    finally:
        plot.engine.stop()
