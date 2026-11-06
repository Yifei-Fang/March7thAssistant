"""
自动对话模块 — 场景检测器。

将所有"判断当前画面处于什么状态"的逻辑集中在此，
与点击执行逻辑解耦，便于单独测试和调整识别参数。
"""

from __future__ import annotations

import time
from typing import Optional

from module.automation import auto

from ._config import Crop, Img, Threshold
from ._dialog_policy import (
    CHOICE_KIND_EXIT,
    CHOICE_KIND_MESSAGE,
    CHOICE_KIND_PREV_PAGE,
    CHOICE_KIND_SELECT,
    ChoiceMark,
    DialogSignals,
    mark_from_match,
)


class SceneDetector:
    """游戏画面场景检测器。

    所有方法均为静态方法，不持有状态，仅依赖 Automation 实例 (auto)
    进行截图和模板匹配。
    """

    # ========================================================================
    # 对话场景判断
    # ========================================================================

    @staticmethod
    def is_dialog_scene() -> bool:
        """判断当前画面是否处于对话场景。

        检测逻辑（按优先级）：
        1. 左上角是否存在 start / start_ps5 / start_xbox 任意一个图标
           → 说明刚进入对话，等待推进
        2. 底部中央是否存在 continue 箭头
           → 说明对话正在展示中
        3. 右上角是否存在 hide 按钮（隐藏 UI）
           → 说明对话正在展示中

        返回:
            True  当前处于对话场景
            False 当前不处于对话场景
        """
        # 截取左上角区域用于 start 系列图标检测
        auto.take_screenshot(crop=Crop.DIALOG_START)

        # 1. 检测 start 图标（键鼠 / PS5 / Xbox 任一命中即可）
        for img_path in Img.START:
            if auto.find_element(img_path, "image", Threshold.START_MATCH, take_screenshot=False):
                return True

        # 2. 检测 continue 箭头（底部中央）
        if auto.find_element(
            Img.CONTINUE, "image", Threshold.CONTINUE_MATCH,
            crop=Crop.DIALOG_CONTINUE,
        ):
            return True

        # 3. 检测 hide 按钮 → 进一步确认对话选项
        if auto.find_element(
            Img.HIDE, "image", Threshold.HIDE_MATCH,
            crop=Crop.DIALOG_HIDE,
        ):
            return True

        return False

    # ========================================================================
    # 跳过按钮检测
    # ========================================================================

    @staticmethod
    def detect_skip_button() -> bool:
        """检测并点击跳过按钮（使用 YOLO 模型）。

        步骤：
        1. 在右上角区域用 YOLO 检测 skip 目标
        2. 若检测到并点击成功，轮询等待按钮消失（最多 1 秒）
        3. 按钮消失 = 跳过成功

        返回:
            True  成功检测并点击跳过按钮，且按钮已消失
            False 未检测到跳过按钮，或点击后按钮未消失
        """
        clicked = auto.click_element(
            target=Img.SKIP_MODEL,
            find_type="yolo",
            threshold=Threshold.SKIP_YOLO,
            crop=Crop.SKIP_BUTTON,
        )
        if not clicked:
            return False

        # 等待跳过按钮消失（最多 1 秒，每 50ms 检查一次）
        deadline = time.monotonic() + Threshold.SKIP_WAIT_TIMEOUT
        while time.monotonic() < deadline:
            time.sleep(Threshold.SKIP_POLL_INTERVAL)
            if not auto.find_element(
                Img.SKIP_MODEL, "yolo", Threshold.SKIP_YOLO,
                crop=Crop.SKIP_BUTTON,
            ):
                return True
        return False

    # ========================================================================
    # 确认弹窗检测
    # ========================================================================

    @staticmethod
    def click_confirm(max_retries: int = Threshold.CONFIRM_MAX_RETRIES,
                      retry_delay: float = Threshold.CONFIRM_RETRY_DELAY) -> bool:
        """检测并点击确认按钮。

        返回:
            True  点击成功
            False 重试耗尽仍未点击到
        """
        return auto.click_element(
            Img.CONFIRM, "image", Threshold.CONFIRM_MATCH,
            max_retries=max_retries,
            retry_delay=retry_delay,
        )

    # ========================================================================
    # 对话层观察（只识别，不点击）
    # ========================================================================

    @staticmethod
    def observe_dialog() -> DialogSignals:
        """采集当前画面对话层信号。

        本方法不点击。选项图标在宽检测带里一次取全，供策略判断是不是单列。
        """
        auto.take_screenshot()
        dialog_chrome = _seen_any(Img.START, Threshold.START_MATCH) or _seen(Img.HIDE, Threshold.HIDE_MATCH)
        continue_visible = _seen(Img.CONTINUE, Threshold.CONTINUE_MATCH)
        confirm_visible = _seen(Img.CONFIRM, Threshold.CONFIRM_MATCH)
        teleport_visible = _seen_any(Img.TELEPORT, Threshold.STOP_UI_MATCH)
        party_setup_visible = _seen_any(Img.PARTY_SETUP, Threshold.STOP_UI_MATCH)
        challenge_start_visible = _seen_any(Img.CHALLENGE_START, Threshold.STOP_UI_MATCH)
        system_ui_visible = _seen_any(Img.SYSTEM_UI, Threshold.STOP_UI_MATCH)
        phone_visible = _seen(Img.CLOSE_PHONE, Threshold.CLOSE_PHONE_MATCH) or _seen_any(
            Img.PHONE_UI, Threshold.STOP_UI_MATCH,
        )
        combat_visible = _seen_any(Img.COMBAT_CHROME, Threshold.STOP_UI_MATCH)

        auto.take_screenshot(crop=Crop.CHOICE_BAND)
        marks: list[ChoiceMark] = []
        for kind, image in (
            (CHOICE_KIND_SELECT, Img.SELECT),
            (CHOICE_KIND_EXIT, Img.EXIT),
            (CHOICE_KIND_MESSAGE, Img.MESSAGE),
            (CHOICE_KIND_PREV_PAGE, Img.PREV_PAGE),
        ):
            marks.extend(_choice_marks(kind, image))

        investigate_visible = bool(auto.find_element(
            Img.INVESTIGATE, "image", Threshold.INVESTIGATE_MATCH, crop=Crop.INVESTIGATE,
        ))
        skip_visible = bool(auto.find_element(
            Img.SKIP_MODEL, "yolo", Threshold.SKIP_YOLO, crop=Crop.SKIP_BUTTON,
        ))
        phone_visible = phone_visible or auto.is_rgb_ratio_above_threshold(
            Crop.PHONE_MSG, Threshold.PHONE_RGB,
            Threshold.PHONE_RGB_RATIO, tolerance=Threshold.RGB_TOLERANCE,
        ) or auto.is_rgb_ratio_above_threshold(
            Crop.PHONE_MSG2, Threshold.PHONE_RGB,
            Threshold.PHONE2_RGB_RATIO, tolerance=Threshold.RGB_TOLERANCE,
        )
        combat_visible = combat_visible or SceneDetector.is_auto_battle_off()

        return DialogSignals(
            skip_visible=skip_visible,
            dialog_chrome=dialog_chrome,
            continue_visible=continue_visible,
            marks=tuple(marks),
            confirm_visible=confirm_visible,
            teleport_visible=teleport_visible,
            phone_visible=phone_visible,
            combat_visible=combat_visible,
            party_setup_visible=party_setup_visible,
            challenge_start_visible=challenge_start_visible,
            investigate_visible=investigate_visible,
            system_ui_visible=system_ui_visible,
        )

    @staticmethod
    def click_choice(mark: ChoiceMark) -> bool:
        """点击策略已经选定的那一个选项图标。没有坐标时不点击。"""
        if mark.kind != CHOICE_KIND_SELECT or not mark.box:
            return False
        return bool(auto.click_element_with_pos(mark.box))

    @staticmethod
    def is_click_blocked_screen() -> bool:
        """非对话画面里，这些界面不允许自动点关闭或短信。"""
        auto.take_screenshot()
        if _seen_any(Img.COMBAT_CHROME, Threshold.STOP_UI_MATCH):
            return True
        if _seen_any(Img.PARTY_SETUP, Threshold.STOP_UI_MATCH):
            return True
        if _seen_any(Img.CHALLENGE_START, Threshold.STOP_UI_MATCH):
            return True
        if _seen_any(Img.TELEPORT, Threshold.STOP_UI_MATCH):
            return True
        if _seen(Img.CONFIRM, Threshold.CONFIRM_MATCH):
            return True
        return bool(auto.find_element(
            Img.INVESTIGATE, "image", Threshold.INVESTIGATE_MATCH, crop=Crop.INVESTIGATE,
        ))

    # ========================================================================
    # 自动战斗检测
    # ========================================================================

    @staticmethod
    def is_auto_battle_off() -> bool:
        """检查自动战斗是否处于关闭状态。

        在左下角区域检测"未自动战斗"图标。

        返回:
            True  自动战斗未开启
            False 自动战斗已开启或未检测到
        """
        return auto.find_element(
            Img.NOT_AUTO, "image", Threshold.NOT_AUTO_MATCH,
            crop=Crop.NOT_AUTO,
        )

    # ========================================================================
    # 手机对话页面检测
    # ========================================================================

    @staticmethod
    def handle_phone_dialogs() -> None:
        """检测并关闭手机对话页面。

        检测两种手机消息窗口（灰白色背景区域），
        若存在则点击该区域然后移动鼠标到安全位置，
        并尝试点击关闭按钮。
        """
        # 检测第一种手机消息窗口
        if auto.is_rgb_ratio_above_threshold(
            Crop.PHONE_MSG, Threshold.PHONE_RGB,
            Threshold.PHONE_RGB_RATIO, tolerance=Threshold.RGB_TOLERANCE,
        ):
            auto.click_element(Crop.PHONE_MSG, "crop")
            auto.click_element(Crop.WHITE_CORNER, "crop", action="move")

        # 关闭手机按钮
        auto.click_element(
            Img.CLOSE_PHONE, "image", Threshold.CLOSE_PHONE_MATCH,
            crop=Crop.CLOSE_PHONE,
        )

        # 检测第二种手机消息窗口
        if auto.is_rgb_ratio_above_threshold(
            Crop.PHONE_MSG2, Threshold.PHONE_RGB,
            Threshold.PHONE2_RGB_RATIO, tolerance=Threshold.RGB_TOLERANCE,
        ):
            auto.click_element(Crop.PHONE_MSG2, "crop")
            auto.click_element(Crop.WHITE_CORNER, "crop", action="move")

    # ========================================================================
    # 通用关闭与杂项
    # ========================================================================

    @staticmethod
    def click_close() -> None:
        """点击通用关闭按钮（关闭弹窗/页面）。"""
        auto.click_element(Img.CLICK_CLOSE, "image", Threshold.CLICK_CLOSE_MATCH)

    @staticmethod
    def click_continue() -> bool:
        """点击 continue 箭头推进对话。

        返回:
            True  点击成功
            False 未找到 continue 箭头
        """
        return auto.click_element(
            Img.CONTINUE, "image", Threshold.CONTINUE_MATCH,
            crop=Crop.DIALOG_CONTINUE,
        )

    @staticmethod
    def move_mouse_to_click_pos() -> None:
        """将鼠标移动到默认点击位置（底部中央），避免干扰后续匹配。"""
        auto.click_element(Crop.CLICK_POS, "crop", action="move")


def _seen(image: str, threshold: float) -> bool:
    return bool(auto.find_element(image, "image", threshold, take_screenshot=False))


def _seen_any(images: tuple[str, ...], threshold: float) -> bool:
    return any(_seen(image, threshold) for image in images)


def _choice_marks(kind: str, image: str) -> list[ChoiceMark]:
    found = auto.find_element(
        image,
        "image_with_multiple_targets",
        Threshold.CHOICE_MARK_MATCH,
        take_screenshot=False,
        relative=True,
    )
    if not found:
        return []

    width, height = auto.screenshot.size
    pos = auto.screenshot_pos
    scale = auto.screenshot_scale_factor or 1
    return [
        mark_from_match(kind, Crop.CHOICE_BAND, (width, height), top_left, bottom_right, pos, scale)
        for top_left, bottom_right in found
    ]
