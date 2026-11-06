"""A1 对话层决策。

只根据已经识别出的界面信号决定动作，不截图、不点击。
允许的点击只有两种：跳过，或单列对话选项的第一项。
其余界面停止点击，由调用方记下界面类型并等待恢复。

跳过确认框不在这里点。调用方仅在本轮刚刚点中跳过按钮之后，
才按确认模板点击，避免把传送/确认取消框的第一项当成选项。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import Sequence


class DialogUiType(str, Enum):
    """停止或点击时写入日志的界面类型。"""

    SKIPPABLE_DIALOG = "skippable_dialog"
    SINGLE_COLUMN_CHOICE = "single_column_choice"
    STANDING_DIALOG = "standing_dialog"
    DIALOG_TEXT_PENDING = "dialog_text_pending"
    MULTI_COLUMN_CHOICE = "multi_column_choice"
    NONSTANDARD_CHOICE = "nonstandard_choice"
    CUTSCENE = "cutscene"
    INVESTIGATE = "investigate"
    COMBAT_ENTRY = "combat_entry"
    CONFIRM_CANCEL = "confirm_cancel"
    TELEPORT = "teleport"
    PHONE_SMS = "phone_sms"
    PARTY_SETUP = "party_setup"
    CHALLENGE_START = "challenge_start"
    SYSTEM_UI = "system_ui"
    UNKNOWN_BUTTON = "unknown_button"


class DialogAction(str, Enum):
    CLICK_SKIP = "click_skip"
    CLICK_FIRST_OPTION = "click_first_option"
    CLICK_CONTINUE = "click_continue"
    WAIT = "wait"
    STOP = "stop"


# 同一列选项图标的横向偏差。超过屏幕宽度的 4% 视为多列，不再猜第一项。
COLUMN_X_TOLERANCE = 0.04
# 同一枚图标会被模板匹配命中多次，这个距离内的重复点合并。
MARK_DEDUPE_DISTANCE = 0.02

CHOICE_KIND_SELECT = "select"
CHOICE_KIND_EXIT = "exit"
CHOICE_KIND_MESSAGE = "message"
CHOICE_KIND_PREV_PAGE = "prev_page"


@dataclass(frozen=True)
class ChoiceMark:
    """一个选项图标。x/y 是屏幕宽高的比例，box 是可点击的绝对坐标。"""

    kind: str
    x: float
    y: float
    box: tuple = ()


@dataclass(frozen=True)
class DialogSignals:
    skip_visible: bool = False
    dialog_chrome: bool = False
    continue_visible: bool = False
    marks: tuple[ChoiceMark, ...] = ()
    confirm_visible: bool = False
    teleport_visible: bool = False
    phone_visible: bool = False
    combat_visible: bool = False
    party_setup_visible: bool = False
    challenge_start_visible: bool = False
    investigate_visible: bool = False
    system_ui_visible: bool = False
    unknown_button_visible: bool = False


@dataclass(frozen=True)
class DialogDecision:
    action: DialogAction
    ui_type: DialogUiType
    first_option: ChoiceMark | None = None


def decide_dialog_action(
    signals: DialogSignals,
    *,
    auto_skip: bool,
    auto_click: bool,
) -> DialogDecision:
    """决定本轮对话动作。

    不可点击的系统界面优先于跳过：那些画面上的“第一项”可能是取消。
    其后才是跳过、单列第一项、普通对话的继续箭头。
    """
    blocked = _blocked_screen(signals)
    if blocked is not None:
        return blocked

    if auto_skip and signals.skip_visible:
        return DialogDecision(DialogAction.CLICK_SKIP, DialogUiType.SKIPPABLE_DIALOG)

    marks = _dedupe(signals.marks)
    if marks:
        return _decide_choices(marks, auto_click=auto_click)

    if signals.continue_visible:
        return DialogDecision(DialogAction.CLICK_CONTINUE, DialogUiType.STANDING_DIALOG)

    if signals.dialog_chrome:
        return DialogDecision(DialogAction.WAIT, DialogUiType.DIALOG_TEXT_PENDING)

    return DialogDecision(DialogAction.WAIT, DialogUiType.CUTSCENE)


def _blocked_screen(signals: DialogSignals) -> DialogDecision | None:
    checks = (
        (signals.investigate_visible, DialogUiType.INVESTIGATE),
        (signals.party_setup_visible, DialogUiType.PARTY_SETUP),
        (signals.challenge_start_visible, DialogUiType.CHALLENGE_START),
        (signals.combat_visible, DialogUiType.COMBAT_ENTRY),
        (signals.teleport_visible, DialogUiType.TELEPORT),
        (signals.phone_visible, DialogUiType.PHONE_SMS),
        (signals.confirm_visible, DialogUiType.CONFIRM_CANCEL),
        (signals.system_ui_visible, DialogUiType.SYSTEM_UI),
        (signals.unknown_button_visible, DialogUiType.UNKNOWN_BUTTON),
    )
    for visible, ui_type in checks:
        if visible:
            return DialogDecision(DialogAction.STOP, ui_type)
    return None


def _decide_choices(marks: Sequence[ChoiceMark], *, auto_click: bool) -> DialogDecision:
    kinds = {mark.kind for mark in marks}
    if CHOICE_KIND_MESSAGE in kinds:
        return DialogDecision(DialogAction.STOP, DialogUiType.PHONE_SMS)
    if CHOICE_KIND_PREV_PAGE in kinds or CHOICE_KIND_EXIT in kinds or not kinds <= {CHOICE_KIND_SELECT}:
        return DialogDecision(DialogAction.STOP, DialogUiType.NONSTANDARD_CHOICE)

    selects = [mark for mark in marks if mark.kind == CHOICE_KIND_SELECT]
    if not selects or any(not _finite_mark(mark) for mark in selects):
        return DialogDecision(DialogAction.STOP, DialogUiType.NONSTANDARD_CHOICE)

    span = max(mark.x for mark in selects) - min(mark.x for mark in selects)
    if span > COLUMN_X_TOLERANCE:
        return DialogDecision(DialogAction.STOP, DialogUiType.MULTI_COLUMN_CHOICE)

    if not auto_click:
        return DialogDecision(DialogAction.WAIT, DialogUiType.SINGLE_COLUMN_CHOICE)

    first = min(selects, key=lambda mark: (mark.y, mark.x))
    return DialogDecision(
        DialogAction.CLICK_FIRST_OPTION,
        DialogUiType.SINGLE_COLUMN_CHOICE,
        first,
    )


def _dedupe(marks: Sequence[ChoiceMark]) -> list[ChoiceMark]:
    kept: list[ChoiceMark] = []
    for mark in sorted(marks, key=lambda item: (item.y, item.x, item.kind)):
        if not _finite_mark(mark):
            kept.append(mark)
            continue
        if any(
            other.kind == mark.kind
            and _finite_mark(other)
            and abs(other.x - mark.x) <= MARK_DEDUPE_DISTANCE
            and abs(other.y - mark.y) <= MARK_DEDUPE_DISTANCE
            for other in kept
        ):
            continue
        kept.append(mark)
    return kept


def _finite_mark(mark: ChoiceMark) -> bool:
    return math.isfinite(mark.x) and math.isfinite(mark.y)


def mark_from_match(
    kind: str,
    crop: tuple[float, float, float, float],
    image_size: tuple[int, int],
    top_left: tuple[int, int],
    bottom_right: tuple[int, int],
    screenshot_pos: tuple | None,
    scale: float,
) -> ChoiceMark:
    """把裁剪图里的一次模板命中换成屏幕比例和绝对点击框。

    没有窗口位置时不生成点击框，调用方不得自己发明坐标。
    """
    width, height = image_size
    if width <= 0 or height <= 0:
        raise RuntimeError("选项区域截图尺寸无效")

    crop_x, crop_y, crop_w, crop_h = crop
    center_x = (top_left[0] + bottom_right[0]) / 2
    center_y = (top_left[1] + bottom_right[1]) / 2
    screen_x = crop_x + (center_x / width) * crop_w
    screen_y = crop_y + (center_y / height) * crop_h

    box: tuple = ()
    if screenshot_pos:
        safe_scale = scale if scale and scale > 0 else 1
        box = (
            (
                int(top_left[0] / safe_scale) + screenshot_pos[0],
                int(top_left[1] / safe_scale) + screenshot_pos[1],
            ),
            (
                int(bottom_right[0] / safe_scale) + screenshot_pos[0],
                int(bottom_right[1] / safe_scale) + screenshot_pos[1],
            ),
        )
    return ChoiceMark(kind, screen_x, screen_y, box)
