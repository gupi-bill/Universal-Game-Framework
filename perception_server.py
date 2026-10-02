#!/usr/bin/env python3
"""
Universal-Game-Framework 感知服务 perception_server.py
======================================================
监听 127.0.0.1:<server.perception_port>（默认 5001），供 MCP Agent 通过 perceive_game 调用。

输出统一结构化 JSON：player, entities, teammates, afk_popup。

v2.0 S6 · 离线化改造
--------------------
在无 YOLO、无显卡、无 X server、无游戏窗口的机器上，本服务仍可被拉起并返回
符合契约的结构化结果，整条 detect→brief→play 链路可跑通：

- 后端可选：`auto` / `http` / `mock`
  - `auto`（默认）：探测到截图工具 + YOLO 检测脚本才走 http，否则自动降级 mock
  - `http`：强制真实截图 + YOLO；任一环节不可用都返回**明确错误**，不崩溃
  - `mock`：直接返回 `perception.mock` 配置里的合成场景，零外部依赖
  - 环境变量 `UGF_PERCEPTION_BACKEND` 优先级高于 config.yaml
- mock 实体可随时间按速度漂移，供 `predictor` 累积多帧、验证预判链路
- 自检：`python perception_server.py --selftest [--backend mock] [--rounds 3]`

其它既有保证：
- 截图用完立刻删除，不占硬盘
- YOLO 卡住/CPU 满载时超时跳过，不崩溃
- 截图失败静默丢弃，不写错误文件
- 自动过滤非法/负数/越界/NaN 坐标实体
"""
import json
import math
import os
import shutil
import tempfile
import subprocess
import sys
import time

from flask import Flask, jsonify

app = Flask(__name__)

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FLORR_TOOLS_DIR = os.path.join(BASE_DIR, "florr_powerful_tools")
SCREENSHOT_PATH = os.path.join(BASE_DIR, ".perception_frame.png")

BACKEND_ENV = "UGF_PERCEPTION_BACKEND"
VALID_BACKENDS = ("auto", "http", "mock")

# mock 漂移状态：记录上一次采样时间，用于按速度推进实体坐标
_MOCK_STATE = {"last": None}


def _cfg(path, default=None):
    """
    运行时读取配置（不缓存），保证 config 热加载对新请求立即生效。
    config 缺失或异常时回退 default，绝不让感知服务因配置问题起不来。
    """
    try:
        import config
        return config.get(path, default)
    except Exception:
        return default


def _f(value, default=0.0):
    """安全浮点：None / 字符串 / NaN / Inf 一律回退 default。"""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(v):
        return default
    return v


def _i(value, default=0):
    return int(_f(value, default))


def port() -> int:
    """感知服务端口：perception.port 优先，其次 server.perception_port。"""
    p = _i(_cfg("perception.port", 0), 0)
    if p <= 0:
        p = _i(_cfg("server.perception_port", 5001), 5001)
    return p


def yolo_timeout() -> int:
    return max(1, _i(_cfg("perception.yolo_timeout", 8), 8))


def skip_on_timeout() -> float:
    return max(0.0, _f(_cfg("perception.skip_on_timeout", 0.8), 0.8))


def screen_size():
    """越界过滤用的屏幕尺寸（默认回退 combat.safe_zone_*，再回退 1920x1080）。"""
    w = _f(_cfg("perception.screen_w", _cfg("combat.safe_zone_w", 1920)), 1920)
    h = _f(_cfg("perception.screen_h", _cfg("combat.safe_zone_h", 1080)), 1080)
    if w <= 0:
        w = 1920.0
    if h <= 0:
        h = 1080.0
    return w, h


# ---------------------------------------------------------------------------
# 后端选择
# ---------------------------------------------------------------------------
def configured_backend() -> str:
    """环境变量 > config.yaml > auto。非法值一律回落 auto。"""
    env = str(os.environ.get(BACKEND_ENV, "")).strip().lower()
    if env in VALID_BACKENDS:
        return env
    b = str(_cfg("perception.backend", "auto")).strip().lower()
    return b if b in VALID_BACKENDS else "auto"


#: 候选截图程序，按「跨桌面环境可靠性」排序。
#:
#: v2.0 真机实测发现两个问题：
#:   1. 旧顺序把 ImageMagick 的 `import` 排在 `gnome-screenshot` 前面。
#:      本机 `import` 已损坏（退出码非 0、不产文件），于是探测阶段
#:      「看它存在」就选了它，真正截图时必然失败 —— 而 `gnome-screenshot`
#:      是好的，却被跳过。
#:   2. Wayland 会话下 XWayland 通常在 `:1`，`:0` 反而可能没有授权。
#:      所以不再信任继承来的 DISPLAY。
#:
#: 现在的做法：**挨个真试一遍**，用第一个能产出文件的。
#: 探测阶段不再靠 shutil.which 猜。
#:
#: 顺序按**本机实测延迟**排（Xorg 会话，2026-10-02，负载<1）：
#:
#:   mss              32 ms   纯库，无子进程
#:   xwd             201 ms   原始格式，需再转码
#:   import         1443 ms
#:   gnome-screenshot 2303 ms  GNOME 桌面集成，最慢
#:
#: 原顺序是 gnome-screenshot 打头 —— 那是在 Wayland 下测的，
#: 当时它是**唯一**能用的（import/xwd/mss 在 rootless Xwayland 上
#: 全部 BadMatch / XProtoError）。换到 Xorg 后它变成了最慢的，
#: 顺序必须跟着换，否则每帧都在用最贵的那个。
_MSS_AVAILABLE = None


def _mss_available() -> bool:
    """mss 能否 import（只探一次，结果缓存）。"""
    global _MSS_AVAILABLE
    if _MSS_AVAILABLE is None:
        try:
            import mss  # noqa: F401
            _MSS_AVAILABLE = True
        except Exception:
            _MSS_AVAILABLE = False
    return _MSS_AVAILABLE


def _screenshot_with_mss(path: str) -> bool:
    """用 mss 库截图，绕开子进程。

    需要 Xorg/X11 会话：在 GNOME on Wayland + Xwayland -rootless 下，
    mss 会在枚举显示器时抛 XProtoError（实测），所以这里把异常
    一律吞掉返回 False，交给后面的候选工具。
    """
    if not _mss_available():
        return False
    try:
        import mss
        import mss.tools
    except Exception:
        return False
    for display in _displays_to_try():
        if not display:
            continue
        env_backup = os.environ.get("DISPLAY")
        try:
            os.environ["DISPLAY"] = display
            with mss.MSS() as sct:
                shot = sct.grab(sct.monitors[1])
                if os.path.exists(path):
                    os.remove(path)
                mss.tools.to_png(shot.rgb, shot.size, output=path)
            if os.path.exists(path) and os.path.getsize(path) > 0:
                return True
        except Exception:
            continue
        finally:
            if env_backup is None:
                os.environ.pop("DISPLAY", None)
            else:
                os.environ["DISPLAY"] = env_backup
    return False


#: 按实测延迟排序：mss → xwd → import → gnome-screenshot
_SCREENSHOT_CANDIDATES = ("mss", "xwd", "import", "gnome-screenshot", "scrot", "maim")


def _screenshot_cmd(tool: str, path: str) -> list:
    """按工具名拼出命令行。"""
    if tool == "xwd":
        return [tool, "-root", "-out", path]
    if tool == "gnome-screenshot":
        return [tool, "-f", path]
    if tool == "scrot":
        return [tool, "-o", path]
    if tool == "import":
        return [tool, "-window", "root", path]
    if tool == "maim":
        return [tool, path]
    return [tool, path]


def _displays_to_try() -> list:
    """候选 DISPLAY 值。

    继承来的先试（用户可能已经设对了），再试 Xwayland 常见的 :1。
    """
    out = []
    cur = os.environ.get("DISPLAY")
    if cur:
        out.append(cur)
    for d in (":1", ":0"):
        if d not in out:
            out.append(d)
    return out


def _try_screenshot(tool: str, path: str) -> bool:
    """用指定工具 + 指定 DISPLAY 截一次图，产出文件才算成功。"""
    if tool == "mss":
        return _screenshot_with_mss(path)
    if not shutil.which(tool):
        return False
    env = dict(os.environ)
    for display in _displays_to_try():
        if not display:
            continue
        env["DISPLAY"] = display
        try:
            if os.path.exists(path):
                os.remove(path)
            subprocess.run(_screenshot_cmd(tool, path), check=True,
                           capture_output=True, timeout=8, env=env)
            if os.path.exists(path) and os.path.getsize(path) > 0:
                return True
        except Exception:
            continue
    return False


def _screenshot_tool() -> str:
    """挨个试候选截图程序，返回第一个真正能用的。

    与旧实现的关键区别：旧版只看「程序在不在 PATH 里」，
    坏掉的程序也会被选中。本机实测 `import` 存在但不可用，
    旧逻辑必然选它，然后每帧截图都失败。
    """
    for name in _SCREENSHOT_CANDIDATES:
        if _try_screenshot(name, os.path.join(
                tempfile.gettempdir(), f"ugf_probe_{name}.png")):
            return name
    return ""


def resolve_backend() -> str:
    """auto → 有截图工具且找到检测脚本才 http，否则 mock（离线环境必降级）。"""
    be = configured_backend()
    if be != "auto":
        return be
    if _screenshot_tool() and _find_detect_script():
        return "http"
    return "mock"


# ---------------------------------------------------------------------------
# YOLO 调用
# ---------------------------------------------------------------------------
def _find_detect_script() -> str:
    """自动查找 florr_powerful_tools 的检测入口脚本。"""
    candidates = ["detect.py", "main.py", "yolo_detect.py", "infer.py", "run.py"]
    for name in candidates:
        p = os.path.join(FLORR_TOOLS_DIR, name)
        if os.path.exists(p):
            return p
    return ""


def _run_yolo(image_path: str) -> dict:
    """
    调用 YOLO 检测脚本，返回原始检测结果。
    超时返回 {"_timeout": true}，由调用方决定跳过。

    注意：这是**兜底路径**。子进程每帧都要重新 `import ultralytics`
    并 `YOLO(weight)` 加载权重（实测 ~8 秒），会直接撞上
    yolo_timeout(8s) —— 整条 http 感知链路会**每帧超时、一个检测都产不出**。
    正常路径请走 `_yolo_predict()`（模型常驻 + 原始像素直喂）。
    """
    detect_script = _find_detect_script()
    if not detect_script:
        return {"error": f"检测脚本未找到，请在 {FLORR_TOOLS_DIR} 内确认入口文件名"}

    stdout = ""
    try:
        proc = subprocess.run(
            [sys.executable, detect_script, "--image", image_path],
            capture_output=True,
            text=True,
            cwd=FLORR_TOOLS_DIR,
            timeout=yolo_timeout(),
        )
        stdout = (proc.stdout or "").strip()
        if not stdout:
            return {"error": "YOLO 无输出", "stderr": (proc.stderr or "")[-300:]}
        return json.loads(stdout)
    except subprocess.TimeoutExpired:
        return {"_timeout": True}
    except json.JSONDecodeError as e:
        return {"error": f"YOLO 输出不是 JSON: {e}", "raw": stdout[-300:]}
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# 常驻 YOLO（快路径）：模型只加载一次，原始像素直喂，不落盘
# ---------------------------------------------------------------------------
_YOLO = {"model": None, "weight": None, "tried": False, "err": None}


def _yolo_weight() -> str:
    """定位权重，与 detect.py 的候选顺序保持一致。"""
    for d in (os.path.join(BASE_DIR, "models"), os.path.join(BASE_DIR, "weights")):
        if not os.path.isdir(d):
            continue
        for name in sorted(os.listdir(d)):
            if name.endswith(".pt"):
                return os.path.join(d, name)
    return ""


def _silence_cpp(fn, *a, **kw):
    """在 fd 层压制 cv2 的 C++ 日志（NNPACK 等）。

    这些是无害警告，但会刷上百行把 JSON 输出和真正的错误淹掉。
    Python 的 warnings 开关和 OPENCV_LOG_LEVEL 都管不到 std::cerr。
    """
    try:
        sys.stderr.flush()
    except Exception:
        pass
    saved = os.dup(2)
    devnull = os.open(os.devnull, os.O_WRONLY)
    try:
        os.dup2(devnull, 2)
        return fn(*a, **kw)
    finally:
        try:
            sys.stderr.flush()
        except Exception:
            pass
        os.dup2(saved, 2)
        os.close(devnull)
        os.close(saved)


def _yolo_predict(img):
    """用常驻模型对 numpy 图像/BGR 数组做检测，返回 detect.py 同形状的 dict。

    失败返回 {"error": ...}；调用方据此回落到子进程路径。
    """
    if not _YOLO["tried"]:
        _YOLO["tried"] = True
        weight = _yolo_weight()
        if not weight:
            _YOLO["err"] = "找不到权重（models/ 或 weights/ 下无 .pt）"
        else:
            try:
                from ultralytics import YOLO
                _YOLO["model"] = _silence_cpp(YOLO, weight)
                _YOLO["weight"] = weight
            except Exception as e:
                _YOLO["err"] = f"{type(e).__name__}: {str(e)[:120]}"
    if _YOLO["model"] is None:
        return {"error": _YOLO["err"] or "模型未就绪"}

    try:
        res = _silence_cpp(_YOLO["model"].predict, img, verbose=False,
                           imgsz=320, conf=0.30)[0]
    except Exception as e:
        return {"error": f"推理失败 {type(e).__name__}: {str(e)[:120]}"}

    boxes, names = res.boxes, res.names
    entities, player = [], None
    h, w = (img.shape[0], img.shape[1]) if hasattr(img, "shape") else (768, 1366)
    for i in range(len(boxes)):
        cl = int(boxes.cls[i].item())
        x1, y1, x2, y2 = boxes.xyxy[i].tolist()
        cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
        label = names.get(cl, "?")
        raw_id, rarity = _classify_coco(label, cx / max(1, w), cy / max(1, h))
        item = {"raw_id": raw_id, "rarity": rarity,
                "x": round(cx, 1), "y": round(cy, 1),
                "coco_class": label, "conf": round(float(boxes.conf[i].item()), 3),
                "bbox": [round(v, 1) for v in (x1, y1, x2, y2)]}
        if raw_id == "player" and player is None:
            player = {"alive": True, "hp": 100, "max_hp": 100,
                      "x": round(cx, 1), "y": round(cy, 1),
                      "power_score": 90, "petal_set": "shoot", "talent": "none"}
        else:
            entities.append(item)
    if player is None:
        player = {"alive": True, "hp": 100, "max_hp": 100,
                  "x": round(w * 0.5, 1), "y": round(h * 0.8, 1),
                  "power_score": 80, "petal_set": "shoot",
                  "talent": "none", "_fallback": True}
    return {"backend": "yolo", "weight": os.path.basename(_YOLO["weight"] or ""),
            "imgsz": 320, "detections": len(boxes), "player": player,
            "entities": entities[:10], "teammates": [], "afk_popup": False}


def _capture_array():
    """mss 抓原始像素，返回 (BGR ndarray, w, h)；不可用返回 (None, 0, 0)。

    mss 给的是 RGB。这里转成 BGR —— ultralytics/OpenCV 内部按 BCHW 排布，
    通道顺序错了颜色模型会明显退化。
    """
    if not _mss_available():
        return None, 0, 0
    try:
        import mss
        import numpy as np
    except Exception:
        return None, 0, 0
    env_backup = os.environ.get("DISPLAY")
    for display in _displays_to_try():
        if not display:
            continue
        try:
            os.environ["DISPLAY"] = display
            with mss.MSS() as sct:
                shot = sct.grab(sct.monitors[1])
                rgb = np.frombuffer(shot.rgb, dtype=np.uint8).reshape(shot.height, shot.width, 3)
                bgr = rgb[:, :, ::-1].copy()      # RGB -> BGR，且 copy 使其连续
                return bgr, shot.width, shot.height
        except Exception:
            continue
        finally:
            if env_backup is None:
                os.environ.pop("DISPLAY", None)
            else:
                os.environ["DISPLAY"] = env_backup
    return None, 0, 0


def _classify_coco(label, nx, ny):
    """COCO 类别 -> 项目实体语义。与 detect.py 保持一致（保守，不编稀有度）。"""
    n = (label or "").lower()
    person = {"person", "dog", "cat", "horse", "sheep", "cow", "elephant",
              "bear", "zebra", "giraffe"}
    if n in person:
        return ("player" if ny > 0.6 else "enemy"), "normal"
    return "unknown", "unknown"


# ---------------------------------------------------------------------------
# mock 后端（离线合成场景）
# ---------------------------------------------------------------------------
def reset_mock():
    """重置 mock 漂移基准时间（换局 / 测试隔离时用）。"""
    _MOCK_STATE["last"] = None
    _MOCK_STATE["t0"] = None


def _pingpong(start: float, speed: float, dt: float, lo: float, hi: float) -> float:
    """
    屏幕内往返运动（三角波），保证 mock 实体长时间运行也不会飞出边界。

    修复前实体按 v*t 无限外推：跑几分钟后 x 超出"屏幕 2 倍"的越界阈值被过滤，
    感知结果从 5 个实体退化到 0~1 个，离线链路会假死。
    """
    if hi <= lo:
        return start
    span = float(hi - lo)
    if speed == 0:
        return min(max(start, lo), hi)
    offset = (start - lo) + speed * dt
    period = 2.0 * span
    phase = math.fmod(offset, period)
    if phase < 0:
        phase += period
    pos = phase if phase <= span else period - phase
    return lo + pos


def mock_detections(cfg=None, now=None) -> dict:
    """
    生成符合感知契约的合成检测结果。

    cfg 为 None 时读 `perception.mock` 配置；drift=true 时实体按 vx/vy (px/s)
    随真实时间推进，使 predictor 能累积出带速度的多帧历史。
    """
    if cfg is None:
        cfg = _cfg("perception.mock", {})
    if not isinstance(cfg, dict):
        cfg = {}

    now = time.time() if now is None else float(now)
    dt = 0.0
    if bool(cfg.get("drift", True)):
        # v2.0 S7：基准时间一旦确立就不再随每帧滑动。
        # 修复前 dt 取"上一帧到这一帧"的增量，位置 = start + v*dt 恒等于
        # start + v*帧间隔 —— 实体永远停在原地附近，predictor 算出的速度恒为 0，
        # 预判置信度永远上不去（dry-run 端到端实测：5 帧后 x_now 完全一致）。
        t0 = _MOCK_STATE.get("t0")
        if t0 is None or now < t0:
            t0 = _MOCK_STATE["t0"] = now
        dt = max(0.0, now - t0)
        _MOCK_STATE["last"] = now

    entities = []
    raw_entities = cfg.get("entities")
    w, h = screen_size()
    for e in (raw_entities if isinstance(raw_entities, list) else []):
        if not isinstance(e, dict):
            continue
        x = _pingpong(_f(e.get("x"), 0.0), _f(e.get("vx"), 0.0), dt, 0.0, w)
        y = _pingpong(_f(e.get("y"), 0.0), _f(e.get("vy"), 0.0), dt, 0.0, h)
        entities.append({
            "raw_id": e.get("raw_id", "unknown"),
            "rarity": e.get("rarity", "Common"),
            "x": x,
            "y": y,
            "category": e.get("category", ""),
        })

    player = cfg.get("player") if isinstance(cfg.get("player"), dict) else {}
    teammates = cfg.get("teammates") if isinstance(cfg.get("teammates"), list) else []

    return {
        "player": dict(player),
        "entities": entities,
        "teammates": list(teammates),
        "afk_popup": bool(cfg.get("afk_popup", False)),
        "_mock": True,
    }


# ---------------------------------------------------------------------------
# 实体过滤与标准化
# ---------------------------------------------------------------------------
def _is_valid_entity(ent: dict) -> bool:
    """过滤非法实体：缺字段、负数坐标、越界坐标、NaN/Inf。"""
    if not isinstance(ent, dict):
        return False
    if "raw_id" not in ent or "x" not in ent or "y" not in ent:
        return False
    if not isinstance(ent["x"], (int, float)) and not _numeric_str(ent["x"]):
        return False
    if not isinstance(ent["y"], (int, float)) and not _numeric_str(ent["y"]):
        return False
    x = _f(ent["x"], float("-inf"))
    y = _f(ent["y"], float("-inf"))
    if x < 0 or y < 0:
        return False
    w, h = screen_size()
    if x > w * 2 or y > h * 2:
        return False
    return True


def _numeric_str(v) -> bool:
    """字符串形式的数字（"12.5"）也算合法坐标，但 NaN/Inf 字符串不算。"""
    if not isinstance(v, str):
        return False
    try:
        return math.isfinite(float(v.strip()))
    except (TypeError, ValueError):
        return False


def normalize_entities(raw: list) -> list:
    """标准化实体列表，过滤非法项。"""
    result = []
    if not isinstance(raw, list):
        return result
    for ent in raw:
        if _is_valid_entity(ent):
            result.append({
                "raw_id": str(ent.get("raw_id", "unknown")),
                "rarity": str(ent.get("rarity", "Common")),
                "x": round(_f(ent["x"], 0.0), 1),
                "y": round(_f(ent["y"], 0.0), 1),
            })
    return result


def normalize_player(raw: dict) -> dict:
    """标准化玩家状态（脏值一律回退安全默认，不让 NaN 污染下游决策）。"""
    if not isinstance(raw, dict):
        raw = {}
    return {
        "alive": bool(raw.get("alive", True)),
        "hp": _f(raw.get("hp"), 100.0),
        "max_hp": _f(raw.get("max_hp"), 100.0),
        "x": _f(raw.get("x"), 0.0),
        "y": _f(raw.get("y"), 0.0),
        "power_score": _f(raw.get("power_score"), 100.0),
        "petal_set": str(raw.get("petal_set", "combat")),
        "talent": str(raw.get("talent", "none")),
    }


def _is_teammate_ent(ent: dict) -> bool:
    """从实体里粗判是否为队友（同阵营标识：raw_id/分类带 player/ally/team）。"""
    if not isinstance(ent, dict):
        return False
    rid = str(ent.get("raw_id", "")).lower()
    cat = str(ent.get("category", "")).lower()
    return any(k in rid or k in cat
               for k in ("player_ally", "ally", "teammate", "friend", "party"))


def normalize_teammates(raw: dict, all_raw: list) -> list:
    """标准化队友列表（v0.3）：优先取 raw.teammates，其次从实体中按同阵营标识识别。"""
    result = []
    sources = []
    if isinstance(raw, dict):
        rt = raw.get("teammates")
        if isinstance(rt, list):
            sources = list(rt)
    if not sources:
        sources = [e for e in (all_raw if isinstance(all_raw, list) else [])
                   if _is_teammate_ent(e)]
    for tm in sources:
        if not isinstance(tm, dict):
            continue
        try:
            result.append({
                "raw_id": str(tm.get("raw_id", "player_ally")),
                "petal_set": str(tm.get("petal_set", "combat")),
                "x": round(_f(tm.get("x"), 0.0), 1),
                "y": round(_f(tm.get("y"), 0.0), 1),
            })
        except (TypeError, ValueError):
            continue
    return result


def normalize_detections(det: dict) -> dict:
    """把任意后端返回的原始检测结果标准化为对外契约。"""
    if not isinstance(det, dict):
        det = {}
    player = normalize_player(det.get("player", {}))
    raw_entities = det.get("entities", det.get("monsters", []))
    if not isinstance(raw_entities, list):
        raw_entities = []
    entities = normalize_entities(raw_entities)
    # 队友会从 entities 里排掉，避免被当作敌人
    entities = [e for e in entities if not _is_teammate_ent(e)]
    teammates = normalize_teammates(det, raw_entities)
    return {
        "player": player,
        "entities": entities,
        "teammates": teammates,
        "afk_popup": bool(det.get("afk_popup", False)),
        "_raw": det,
    }


# 保持向后兼容的私有别名（既有调用方/测试可能引用）
_normalize_entities = normalize_entities
_normalize_player = normalize_player
_normalize_teammates = normalize_teammates
_normalize_detections = normalize_detections


def _empty_player() -> dict:
    return {"alive": True, "hp": 100, "max_hp": 100, "x": 0, "y": 0}


def _skip_payload(reason: str, message: str = "", backend: str = "") -> dict:
    """
    统一的"这一帧不可用"载荷。

    v2.0 S6：原实现把 YOLO 报错伪装成"空场景"（entities=[]），Agent 会误判为
    "游戏里没怪"而继续空转。现在显式带 _skipped / _reason / _error 三个键。
    """
    payload = {
        "player": _empty_player(),
        "entities": [],
        "teammates": [],
        "afk_popup": False,
        "_skipped": True,
        "_reason": reason,
    }
    if message:
        payload["_error"] = message
    if backend:
        payload["_backend"] = backend
    return payload


# ---------------------------------------------------------------------------
# 帧采集
# ---------------------------------------------------------------------------
def _take_screenshot(path: str) -> bool:
    """截图成功返回 True。无截图工具/执行失败返回 False（不写错误文件）。

    挨个候选工具真试（而不是只探存在性），并逐个试 DISPLAY。
    """
    for name in _SCREENSHOT_CANDIDATES:
        if _try_screenshot(name, path):
            return True
    return False


def build_perception_payload(backend_name: str = None, now=None) -> dict:
    """
    生成一帧感知结果（路由与自检共用同一条路径，避免"自检能过、实际跑挂"）。
    """
    be = (backend_name or resolve_backend()).strip().lower()
    if be not in ("http", "mock"):
        be = resolve_backend()

    if be == "mock":
        payload = normalize_detections(mock_detections(now=now))
        payload["_backend"] = "mock"
        payload["_mock"] = True
        return payload

    # ---- http 后端：真实截图 + YOLO ----
    #
    # 快路径：mss 抓原始像素 → 常驻模型直接推理 → 不落盘。
    # 实测把每帧从 ~8200ms（子进程每帧重载权重，必然撞超时）降到 ~1200ms。
    # 任何一环不可用都自动回落到旧的「落盘 + 子进程」路径。
    fastpath_err = None
    bgr, _cap_w, _cap_h = _capture_array()
    if bgr is not None:
        detections = _yolo_predict(bgr)
        if not detections.get("error"):
            payload = normalize_detections(detections)
            payload["_backend"] = "http"
            payload["_fastpath"] = True
            return payload
            # 快路径失败 → 往下走兜底
        fastpath_err = detections["error"]
    if not _screenshot_tool():
        return _skip_payload(
            "screenshot_tool_missing",
            "未找到截图工具(mss/xwd/import/gnome-screenshot)，无头环境请设 UGF_PERCEPTION_BACKEND=mock",
            "http")
    if not _find_detect_script():
        return _skip_payload(
            "detect_script_missing",
            f"未找到 YOLO 检测脚本，请在 {FLORR_TOOLS_DIR} 内放置入口文件",
            "http")
    if not _take_screenshot(SCREENSHOT_PATH):
        return _skip_payload("screenshot_failed",
                             "截图执行失败（无 X server 时属预期）", "http")

    try:
        detections = _run_yolo(SCREENSHOT_PATH)
    finally:
        # 无论成功失败，截图用完立刻删
        if os.path.exists(SCREENSHOT_PATH):
            try:
                os.remove(SCREENSHOT_PATH)
            except OSError:
                pass

    if not isinstance(detections, dict):
        return _skip_payload("yolo_bad_output", "YOLO 返回值不是 dict", "http")
    if detections.get("_timeout"):
        time.sleep(skip_on_timeout())
        return _skip_payload("yolo_timeout",
                             f"YOLO 超过 {yolo_timeout()}s 未返回"
                             + (f"（快路径也不可用：{fastpath_err}）" if fastpath_err else ""),
                             "http")
    if detections.get("error"):
        return _skip_payload("yolo_error", str(detections.get("error")), "http")

    payload = normalize_detections(detections)
    payload["_backend"] = "http"
    return payload


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------
@app.route("/perceive", methods=["GET"])
def perceive():
    """
    mock：直接返回合成场景；http：截图 → YOLO → 标准化 → 删除截图 → 返回 JSON。
    任何不可用环节都返回 _skipped + _reason + _error，Agent 据此跳过本帧。
    """
    payload = build_perception_payload()
    # 自检/调试时可用 ?raw=0 去掉 _raw，减少 token
    if str(app_request_arg("raw", "1")).lower() in ("0", "false", "no"):
        payload.pop("_raw", None)
    return jsonify(payload)


def app_request_arg(key: str, default=None):
    """取查询参数（抽出来便于测试与无请求上下文场景）。"""
    try:
        from flask import request
        return request.args.get(key, default)
    except Exception:
        return default


@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status": "ok",
        "backend": resolve_backend(),
        "configured_backend": configured_backend(),
        "port": port(),
        "florr_tools_dir": FLORR_TOOLS_DIR,
        "detect_script": _find_detect_script(),
        "screenshot_tool": _screenshot_tool(),
        "yolo_timeout": yolo_timeout(),
        "offline": resolve_backend() == "mock",
    })


# ---------------------------------------------------------------------------
# 自检（离线可跑，不退化为"起服务才算通过"）
# ---------------------------------------------------------------------------
CONTRACT_KEYS = ("player", "entities", "teammates", "afk_popup")


def _check_payload(p: dict):
    """校验一帧是否符合契约，返回问题列表（空列表=通过）。"""
    problems = []
    if not isinstance(p, dict):
        return ["payload 不是 dict"]
    for k in CONTRACT_KEYS:
        if k not in p:
            problems.append(f"缺字段 {k}")
    if "player" in p and not isinstance(p.get("player"), dict):
        problems.append("player 不是 dict")
    if "entities" in p and not isinstance(p.get("entities"), list):
        problems.append("entities 不是 list")
    for e in (p.get("entities") or []) if isinstance(p.get("entities"), list) else []:
        if not isinstance(e, dict) or "raw_id" not in e or "x" not in e or "y" not in e:
            problems.append(f"实体结构非法: {e!r}")
    return problems


def selftest(rounds: int = 3, backend_name: str = None) -> int:
    """
    跑 N 帧并检查契约。退出码 0 = 全部通过，1 = 有帧不合规。
    mock 后端还会校验"多帧漂移"是否产生可用速度（predictor 需要 ≥3 帧）。
    """
    be = (backend_name or resolve_backend()).strip().lower()
    if be not in ("http", "mock"):
        be = resolve_backend()
    reset_mock()

    base = time.time()
    frames, problems_all = [], []
    for i in range(max(1, rounds)):
        p = build_perception_payload(be, now=base + i * 0.5)
        problems = _check_payload(p)
        if problems:
            problems_all.append({"round": i, "problems": problems})
        p.pop("_raw", None)
        frames.append(p)

    summary = {
        "backend": be,
        "configured_backend": configured_backend(),
        "port": port(),
        "rounds": len(frames),
        "ok": not problems_all,
        "problems": problems_all,
    }
    if be == "mock" and len(frames) >= 2:
        moved = []
        for a, b in zip(frames, frames[1:]):
            for e1 in a.get("entities", []):
                for e2 in b.get("entities", []):
                    if e1.get("raw_id") == e2.get("raw_id"):
                        moved.append(round(abs(e2["x"] - e1["x"]) + abs(e2["y"] - e1["y"]), 2))
        summary["drift_px"] = moved
        summary["drifting"] = any(m > 0 for m in moved)
    summary["last_frame"] = frames[-1]
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["ok"] else 1


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="UGF 感知服务")
    ap.add_argument("--selftest", action="store_true",
                    help="跑 N 帧契约自检并打印结构化结果（不起服务）")
    ap.add_argument("--rounds", type=int, default=3, help="自检帧数，默认 3")
    ap.add_argument("--backend", choices=list(VALID_BACKENDS), default=None,
                    help="覆盖后端选择（等价于 UGF_PERCEPTION_BACKEND）")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=0, help="0=用配置端口")
    args = ap.parse_args(argv)

    if args.backend:
        os.environ[BACKEND_ENV] = args.backend

    if args.selftest:
        return selftest(args.rounds, args.backend)

    print(f"[感知] backend={resolve_backend()} port={port()} "
          f"offline={resolve_backend() == 'mock'}")
    app.run(host=args.host, port=args.port or port(), debug=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
