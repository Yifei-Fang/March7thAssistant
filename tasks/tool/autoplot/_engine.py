"""
自动对话模块 — 核心引擎。

AutoPlot 是整个自动对话功能的主控制器，负责：
- 管理运行状态与生命周期（启动 / 停止 / 配置更新）
- 通过定时器轮询游戏窗口状态
- 协调场景检测与点击执行
- 核心策略：可跳过的站立对话点跳过；只有单列选项时点第一项；
  其余界面停止点击并记下界面类型，等对话或探索画面回来再继续

架构说明：
    AutoPlot (QObject)          ← 主控制器，持有 QTimer
        ├── SceneDetector       ← 静态方法，场景识别（只观察，不决定点哪里）
        ├── decide_dialog_action ← 对话层策略
        └── _dialog_loop()      ← 按策略执行跳过 / 第一项 / 继续，或停止

会话隔离机制：
    使用递增的 _dialog_session 计数器来取消过期的延迟回调。
    每次进入/退出对话场景时计数器递增，回调执行前检查 session
    是否匹配，不匹配则忽略（防止旧回调干扰新对话）。
"""

from __future__ import annotations

import time
from typing import Callable

try:
    import pygetwindow as gw
except (ImportError, NotImplementedError):
    # pygetwindow 仅支持 Windows，非 Windows 平台降级（见 _is_game_window_active）
    gw = None
from PySide6.QtCore import QObject, QTimer

from module.automation import auto
from module.automation.automation import screenshot_retry_timeout
from module.automation.background_input import input_wait_limit
from module.config import cfg
from module.logger import log

from ._config import (
    EngineState,
    Crop,
    Img,
    Threshold,
    Defaults,
)
from ._detector import SceneDetector
from ._dialog_policy import (
    CHOICE_KIND_SELECT,
    DialogAction,
    DialogDecision,
    DialogSignals,
    DialogUiType,
    decide_dialog_action,
)

# 对话循环仍在跑的状态。HOLDING 表示已经停止点击，但还在等界面恢复。
_DIALOG_CYCLE = (
    EngineState.DIALOG_ACTIVE,
    EngineState.WAITING_OPTION,
    EngineState.HOLDING,
)

# 引擎在图形界面线程上执行点击：后台模式下鼠标正被使用或窗口暂时不可用时只短暂等待，交给下一轮监控，不阻塞界面。
GUI_INPUT_WAIT_LIMIT = 0.3


class AutoPlot(QObject):
    """自动对话引擎。

    使用方式:
        engine = AutoPlot(game_title_name)
        engine.update_options({...})   # 配置选项
        engine.start()                 # 启动
        engine.stop()                  # 停止

    信号:
        本类不直接发射信号；状态变化通过 log 输出。
        调用方可通过 is_running 属性查询运行状态。
    """

    # ========================================================================
    # 构造与初始化
    # ========================================================================

    def __init__(self, game_title_name: str) -> None:
        """初始化自动对话引擎。

        参数:
            game_title_name: 游戏窗口标题，用于判断窗口是否处于前台。
        """
        super().__init__()

        # --- 外部依赖 ---
        self._game_title_name: str = game_title_name

        # --- 运行状态 ---
        self.is_running: bool = False
        """外部可读：引擎是否已启动"""
        self._state: EngineState = EngineState.IDLE
        """内部状态机"""

        # --- 会话隔离 ---
        self._dialog_session: int = 0
        """递增计数器，用于取消过期的延迟回调"""

        # --- 自动战斗冷却 ---
        self._last_auto_battle_check: float = 0.0
        """上次检测自动战斗的时间戳（monotonic 秒）"""
        self._last_error_log_time = float('-inf')
        self._held_notice: tuple | None = None
        """最近一次停止点击日志对应的 (界面类型, 动作)。相同则不重复记录。"""

        # --- 可配置选项（有默认值，可通过 update_options 覆盖）---
        self.auto_skip: bool = Defaults.AUTO_SKIP
        self.auto_click: bool = Defaults.AUTO_CLICK
        self.auto_battle_detect_enable: bool = Defaults.AUTO_BATTLE_DETECT
        self.auto_phone_detect_enable: bool = Defaults.AUTO_PHONE_DETECT
        self.hero_chronicle_enable = False
        self._hero_reader = None

        # --- 定时器 ---
        self._monitor_timer = QTimer(self)
        self._monitor_timer.timeout.connect(self._monitor_loop)

    # ========================================================================
    # 公共 API：生命周期管理
    # ========================================================================

    def start(self) -> None:
        """启动自动对话引擎。

        启动 500ms 间隔的监控定时器，开始检测游戏窗口和对话场景。
        重复调用无副作用。
        """
        if self.is_running:
            return

        auto._init_input()
        self.is_running = True
        self._last_error_log_time = float('-inf')
        self._held_notice = None
        if self._hero_reader is not None:
            self._hero_reader.reset()
        self._transition_to(EngineState.MONITORING)
        self._invalidate_session()
        log.info("自动对话已启动")
        self._monitor_timer.start(Threshold.MONITOR_INTERVAL)

    def stop(self) -> None:
        """停止自动对话引擎。

        停止定时器，取消所有待执行的延迟回调（通过递增 session），
        重置内部状态。重复调用无副作用。
        """
        if not self.is_running:
            return

        self.is_running = False
        self._monitor_timer.stop()
        self._invalidate_session()
        self._held_notice = None
        self._transition_to(EngineState.IDLE)
        auto.input_handler.release_all()
        log.info("自动对话已停止")

    # ========================================================================
    # 公共 API：配置
    # ========================================================================

    def update_options(self, options: dict) -> None:
        """更新引擎配置选项。

        可在引擎运行中动态调用，新配置立即生效。
        仅更新传入的键，未传入的键保持原值。

        参数:
            options: 配置字典，支持的键：
                - auto_skip: bool
                - auto_click: bool
                - auto_battle_detect_enable: bool
        """
        if 'auto_skip' in options:
            self.auto_skip = bool(options['auto_skip'])
        if 'auto_click' in options:
            self.auto_click = bool(options['auto_click'])
        if 'auto_battle_detect_enable' in options:
            self.auto_battle_detect_enable = bool(options['auto_battle_detect_enable'])
        if 'auto_phone_detect_enable' in options:
            self.auto_phone_detect_enable = bool(options['auto_phone_detect_enable'])
        if 'hero_chronicle_enable' in options:
            enabled = bool(options['hero_chronicle_enable'])
            if enabled != self.hero_chronicle_enable and self._hero_reader is not None:
                self._hero_reader.reset()
            self.hero_chronicle_enable = enabled

        log.debug(f"自动对话配置已更新: {options}")

    # ========================================================================
    # 内部：状态管理
    # ========================================================================

    def _transition_to(self, new_state: EngineState) -> None:
        """状态迁移（目前仅用于日志追踪）。"""
        self._state = new_state

    def _invalidate_session(self) -> int:
        """使当前会话失效，取消所有待执行的延迟回调。

        返回:
            新的 session 值。
        """
        self._dialog_session += 1
        return self._dialog_session

    def _is_session_valid(self, session: int) -> bool:
        """检查给定的 session 是否仍然有效。

        对话循环状态包括正在点击、等待用户选择，以及因界面不安全而停止点击。
        """
        return (
            self.is_running
            and self._state in _DIALOG_CYCLE
            and session == self._dialog_session
        )

    def _recover_after_error(self, error: Exception) -> None:
        """取消中断的点击会话，由下一轮监控重新检测，避免假运行状态。"""
        if not self.is_running:
            return
        self._held_notice = None
        self._invalidate_session()
        self._transition_to(EngineState.MONITORING)
        try:
            auto.input_handler.release_all()
        except Exception as cleanup_error:
            self.is_running = False
            self._monitor_timer.stop()
            self._transition_to(EngineState.IDLE)
            log.error(f"自动对话已停止，输入释放失败：{cleanup_error}")
            return
        # 截图持续不可用时限制日志频率，仍然每轮尝试恢复。
        now = time.monotonic()
        if now - self._last_error_log_time >= 30:
            log.warning(f"自动对话本轮检测失败，将自动重试：{error}")
            self._last_error_log_time = now

    # ========================================================================
    # 内部：延迟回调调度
    # ========================================================================

    def _schedule(self, delay_ms: int, callback: Callable[[int], None], session: int) -> None:
        """安排在 delay_ms 毫秒后执行回调，自动绑定 session 有效性检查。

        参数:
            delay_ms: 延迟毫秒数。
            callback: 签名为 callback(session: int) -> None 的回调。
            session: 当前会话标识，回调执行前会验证是否匹配。
        """

        def _guarded() -> None:
            if not self._is_session_valid(session):
                return
            try:
                # 两次监控之间也可能切走或关闭游戏，旧回调不得继续操作。
                if not self._is_game_window_active():
                    self._invalidate_session()
                    self._transition_to(EngineState.MONITORING)
                    return
                with screenshot_retry_timeout(0), input_wait_limit(GUI_INPUT_WAIT_LIMIT):
                    callback(session)
            except Exception as e:
                self._recover_after_error(e)

        QTimer.singleShot(delay_ms, _guarded)

    # ========================================================================
    # 内部：窗口激活检测
    # ========================================================================

    def _is_game_window_active(self) -> bool:
        """检查游戏窗口是否处于前台激活状态。

        返回:
            True  游戏窗口在前台
            False 游戏窗口不在前台或未找到
        """
        if getattr(auto.input_handler, 'is_background', False) is True:
            return auto.input_handler.is_available()
        if gw is None:
            # 非 Windows 平台无法枚举窗口，假定激活以放行后续对话检测
            return True
        # 与截图模块的完整标题匹配保持一致，避免浏览器等同名窗口干扰。
        window = gw.getActiveWindow()
        return window is not None and window.title == self._game_title_name

    # ========================================================================
    # 监控主循环（每 500ms 触发一次）
    # ========================================================================

    def _click_hero_book(self, point):
        left, top, width, height = self._hero_reader.region
        x = left + point[0] * width / 1920
        y = top + point[1] * height / 1080
        auto.click_element_with_pos(((x, y), (x, y)))

    def _monitor_loop(self) -> None:
        """监控定时器回调：主状态机入口。

        逻辑概览:
        1. 检查游戏窗口是否激活
           - 未激活 → 停止对话点击，等待下次轮询
        2. 检测是否处于对话场景
           - 是 → 启动对话点击循环
           - 否 → 执行非对话场景的辅助操作（自动战斗、手机页面等）
        """
        if not self.is_running:
            return

        try:
            # GUI 定时回调中只尝试一次截图，失败后交给下一轮重试。
            with screenshot_retry_timeout(0), input_wait_limit(GUI_INPUT_WAIT_LIMIT):
                self._monitor_tick()
        except Exception as e:
            self._recover_after_error(e)

    def _monitor_tick(self) -> None:
        """执行一轮检测；异常统一由定时器入口恢复。"""
        # 1. 窗口激活检查
        window_active = self._is_game_window_active()
        if not window_active:
            if self._state in _DIALOG_CYCLE:
                self._invalidate_session()
                self._transition_to(EngineState.MONITORING)
            return

        if self.hero_chronicle_enable:
            if self._hero_reader is None:
                from ._hero_chronicle import HeroChronicleReader
                self._hero_reader = HeroChronicleReader(self._click_hero_book, self._game_title_name)
            if self._hero_reader.tick():
                self._invalidate_session()
                self._transition_to(EngineState.MONITORING)
                return

        # 2. 对话场景检测 → 启动或停止对话循环
        if SceneDetector.is_dialog_scene():
            # 仅在尚未进入对话状态时启动新的对话循环。HOLDING 也算对话循环仍在跑。
            if self._state not in _DIALOG_CYCLE:
                self._transition_to(EngineState.DIALOG_ACTIVE)
                session = self._invalidate_session()
                self._schedule(
                    Threshold.DIALOG_LOOP_INITIAL_DELAY,
                    self._dialog_loop,
                    session,
                )
        else:
            # 非对话场景：如果之前在对话中（包括等待选项或已停止点击），则退出
            if self._state in _DIALOG_CYCLE:
                self._end_dialog_cycle(explore=True)

            # 3. 非对话场景下的辅助操作
            self._handle_non_dialog_actions()

    # ========================================================================
    # 非对话场景辅助操作
    # ========================================================================

    def _handle_non_dialog_actions(self) -> None:
        """在非对话场景中执行辅助操作。

        按优先级：
        1. 自动战斗检测（10 秒冷却）
        2. 手机对话页面检测与关闭
        3. 通用关闭按钮
        """
        # 1. 自动战斗检测（带 10 秒冷却）。只按已有的自动战斗热键，不点战斗界面。
        auto_battle_off = SceneDetector.is_auto_battle_off()
        if self.auto_battle_detect_enable and auto_battle_off:
            now = time.monotonic()
            if now - self._last_auto_battle_check >= Threshold.AUTO_BATTLE_COOLDOWN:
                log.info("尝试开启自动战斗")
                auto.press_key(cfg.get_value("hotkey_auto_battle", "v"))
                self._last_auto_battle_check = now

        # 战斗、传送确认、编队、挑战和调查界面不再点短信或关闭，避免把第一项点成取消。
        if auto_battle_off or SceneDetector.is_click_blocked_screen():
            return

        # 2. 手机对话页面（可通过开关控制）
        if self.auto_phone_detect_enable:
            SceneDetector.handle_phone_dialogs()

        # 3. 通用关闭
        SceneDetector.click_close()

    # ========================================================================
    # 对话点击循环
    # ========================================================================

    def _dialog_loop(self, session: int) -> None:
        """按对话层策略执行一轮动作。

        1. 可跳过的站立对话 → 点跳过（本轮点中之后才点跳过确认）
        2. 只有单列 select 选项 → 点最靠上的一项
        3. 普通对话的继续箭头 → 点继续
        4. 其余界面停止点击，日志写明 ui_type，下一轮重新识别
        """
        if not self._is_session_valid(session):
            return

        signals = SceneDetector.observe_dialog()
        if not isinstance(signals, DialogSignals):
            raise RuntimeError(f"对话观察结果无效：{type(signals).__name__}")
        decision = decide_dialog_action(
            signals,
            auto_skip=self.auto_skip,
            auto_click=self.auto_click,
        )
        self._apply_dialog_decision(decision, session)

    def _apply_dialog_decision(self, decision: DialogDecision, session: int) -> None:
        action = decision.action

        if action is DialogAction.CLICK_SKIP:
            self._release_hold(decision)
            self._transition_to(EngineState.DIALOG_ACTIVE)
            # 跳过确认只跟在这一次成功的跳过点击后面，不能留到下一轮去点第一项。
            if SceneDetector.detect_skip_button():
                if not SceneDetector.click_confirm():
                    SceneDetector.move_mouse_to_click_pos()
            self._schedule(Threshold.MONITOR_INTERVAL, self._dialog_loop, session)
            return

        if action is DialogAction.CLICK_FIRST_OPTION:
            mark = decision.first_option
            if mark is None or mark.kind != CHOICE_KIND_SELECT or not mark.box:
                self._hold(DialogDecision(DialogAction.STOP, DialogUiType.NONSTANDARD_CHOICE))
                self._schedule(Threshold.MONITOR_INTERVAL, self._dialog_loop, session)
                return
            self._release_hold(decision)
            self._transition_to(EngineState.DIALOG_ACTIVE)
            SceneDetector.click_choice(mark)
            log.debug("自动对话选择首个选项 ui_type=single_column_choice action=click_first_option")
            self._schedule(Threshold.DIALOG_LOOP_INTERVAL, self._dialog_loop, session)
            return

        if action is DialogAction.CLICK_CONTINUE:
            self._release_hold(decision)
            self._transition_to(EngineState.DIALOG_ACTIVE)
            self._click_continue()
            self._schedule(Threshold.DIALOG_LOOP_INTERVAL, self._dialog_loop, session)
            return

        if action is DialogAction.WAIT and decision.ui_type is DialogUiType.SINGLE_COLUMN_CHOICE:
            self._release_hold(decision)
            if self._state != EngineState.WAITING_OPTION:
                log.debug("根据设置，暂停点击以等待用户选择对话选项")
                self._transition_to(EngineState.WAITING_OPTION)
            self._schedule(Threshold.MONITOR_INTERVAL, self._dialog_loop, session)
            return

        if action is DialogAction.WAIT and decision.ui_type is DialogUiType.DIALOG_TEXT_PENDING:
            self._release_hold(decision)
            self._transition_to(EngineState.DIALOG_ACTIVE)
            self._schedule(Threshold.DIALOG_LOOP_INTERVAL, self._dialog_loop, session)
            return

        self._hold(decision)
        self._schedule(Threshold.MONITOR_INTERVAL, self._dialog_loop, session)

    def _click_continue(self) -> None:
        """点击 continue 箭头推进已经展示完的对话文本。"""
        if auto.click_element(
            Img.CONTINUE, "image", Threshold.CONTINUE_MATCH,
            crop=Crop.DIALOG_CONTINUE,
            action="down",
        ):
            time.sleep(0.2)
            auto.mouse_up()
            log.debug("检测到 continue 箭头，点击推进对话")

    def _hold(self, decision: DialogDecision) -> None:
        """停止点击。同一种界面只记录一次，界面变化后再次记录。"""
        self._transition_to(EngineState.HOLDING)
        notice = (decision.ui_type, decision.action)
        if notice == self._held_notice:
            return
        self._held_notice = notice
        log.warning(
            f"自动对话停止点击 ui_type={decision.ui_type.value} action={decision.action.value}"
        )

    def _release_hold(self, decision: DialogDecision | None = None, *, explore: bool = False) -> None:
        """界面恢复到可以继续处理的状态。"""
        if self._held_notice is None:
            return
        if explore:
            log.info("自动对话解除停止 ui_type=explore action=resume")
        elif decision is not None:
            log.info(
                f"自动对话解除停止 ui_type={decision.ui_type.value} action={decision.action.value}"
            )
        self._held_notice = None

    def _end_dialog_cycle(self, *, explore: bool) -> None:
        if explore:
            self._release_hold(explore=True)
        self._invalidate_session()
        self._transition_to(EngineState.MONITORING)
