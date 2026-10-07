# ===========================================================================
# 7. 动作（拟人化 / 安全区 / dry-run）
# ===========================================================================
VALID_ACTIONS = ("move", "attack", "defend", "synthesize", "idle")


def resolve_set_keys() -> dict:
    """套装名 → 数字键；按档案 combat.sets 声明顺序映射 1~9。"""
    sets = cfg_get("combat.sets")
    if isinstance(sets, (list, tuple)):
        names = [str(s).strip().lower() for s in sets if str(s or "").strip()]
        if names:
            return {n: str(i + 1) for i, n in enumerate(names) if i < 9}
    return {"combat": "1", "tank": "2", "retreat": "3", "chase": "4", "team": "5"}


def normalize_set_name(set_name: str):
    """把决策语义名翻译成本游戏真实套装名；翻不出来返回 None。"""
    keys = resolve_set_keys()
    name = (set_name or "").strip().lower()
    if not name:
        return None
    if name in keys:
        return name
    mapping = cfg_get("combat.set_map")
    if isinstance(mapping, dict):
        target = str(mapping.get(name) or "").strip().lower()
        if target in keys:
            return target
    return None


_DPI_DONE = False


def _enable_windows_dpi():
    """真实键鼠操作前声明 Windows DPI 感知（ROADMAP #19），幂等且静默降级。

    150% 等非整数缩放下，不声明 DPI 感知时 pyautogui 拿到的是虚拟化坐标，
    点击会整体偏移。非 Windows 或声明失败均不影响运行。
    """
    global _DPI_DONE
    if _DPI_DONE:
        return
    _DPI_DONE = True
    if os.name != "nt":
        return
    try:
        import ctypes

        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def _dryrun_log(kind: str, detail: str):
    try:
        os.makedirs(RUN_LOGS, exist_ok=True)
        with open(os.path.join(RUN_LOGS, "dryrun_actions.log"), "a", encoding="utf-8") as f:
            f.write(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\t{kind}\t{detail}\n")
    except OSError:
        pass


def _human_move(x: int, y: int):
    """拟人移动：先到随机中间点，偶发停顿，再微移到位。"""
    if dry_run():
        return
    try:
        import pyautogui
    except ImportError:
        return
    pyautogui.moveTo(x + random.uniform(-25, 25), y + random.uniform(-25, 25), duration=0.04)
    if random.random() < 0.15:
        time.sleep(random.uniform(0.1, 0.3))
    pyautogui.moveTo(x, y, duration=0.06)


def game_action(action_type: str, x=None, y=None) -> str:
    """= 原 game_action。dry-run 下只校验参数 + 落盘，不碰键鼠。"""
    action_type = (action_type or "").lower()
    if action_type not in VALID_ACTIONS:
        return f"未知动作类型: {action_type or '(空)'}，可选 {'/'.join(VALID_ACTIONS)}"
    if action_type == "move" and (x is None or y is None):
        return "move 动作必须提供 x 和 y 坐标"
    coord = f" ({x},{y})" if action_type == "move" else ""
    if dry_run():
        _dryrun_log("action", f"{action_type}{coord}")
        return f"[dry-run] 动作已记录（未真实执行）: {action_type}{coord}"
    try:
        import pyautogui
    except ImportError:
        return "错误: 未安装 pyautogui，请执行 pip install pyautogui"
    _enable_windows_dpi()
    if action_type == "move":
        _human_move(int(x), int(y))
    elif action_type == "attack":
        pyautogui.keyDown("space")
        time.sleep(0.2)
        pyautogui.keyUp("space")
    elif action_type == "defend":
        pyautogui.keyDown("shift")
        time.sleep(0.2)
        pyautogui.keyUp("shift")
    elif action_type == "synthesize":
        pyautogui.press("c")
    else:
        time.sleep(0.1)
    return f"动作执行成功: {action_type}{coord}"


def switch_set(set_name: str) -> str:
    """= 原 switch_set。"""
    keys = resolve_set_keys()
    resolved = normalize_set_name(set_name)
    if resolved is None:
        return f"未知套装: {set_name}，可选 {'/'.join(keys)}"
    key = keys[resolved]
    label = f"{set_name} → {resolved}" if resolved != (set_name or "").strip().lower() else resolved
    if dry_run():
        _dryrun_log("switch_set", f"{label} (按键 {key})")
        return f"[dry-run] 套装切换已记录（未真实执行）: {label} (按键 {key})"
    try:
        import pyautogui
    except ImportError:
        return "错误: 未安装 pyautogui，请执行 pip install pyautogui"
    _enable_windows_dpi()
    pyautogui.press(key)
    return f"已切换套装: {label} (按键 {key})"


def handle_afk() -> str:
    """= 原 handle_afk。"""
    return (
        "AFK 弹窗处理已触发：结合当前画面里弹窗的坐标，"
        "用 `python agent.py action move --x <X> --y <Y>` 点击完成验证。"
    )


def mouse_in_corner(screen_w=1920, screen_h=1080, edge=5) -> bool:
    """鼠标移到屏幕角落 = 安全暂停（人手接管）。"""
    if dry_run() or not bool(cfg_get("agent.corner_pause", True)):
        return False
    try:
        import pyautogui

        x, y = pyautogui.position()
    except Exception:
        return False
    return x <= edge and y <= edge


