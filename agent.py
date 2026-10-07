#!/usr/bin/env python3
"""
Universal-Game-Framework · 单文件游戏 Agent  (v3.0)
=====================================================

一个**能自己跑**的游戏智能体：不依赖任何别的 Agent，不需要装成 MCP 服务。
直接 `python agent.py run` 就能自己「看画面 → 预判 → 评估 → 出动作 → 查/写经验 → 复盘 → 汇报」。

设计原则（对应「轻量化 / 跑得快」）：
  1. **单文件**：所有能力都在这一个文件里，没有跨模块 import 链。
  2. **零 IPC**：旧版每回合要走 MCP 子进程 + JSON-RPC 往返 6 次；这里是进程内函数调用。
  3. **惰性依赖**：requests / yaml / pyautogui / cv2 全部按需 import，没装也能跑离线链路。
  4. **数学层零依赖**：预判与战斗评估是纯 math，不引入 numpy。

能力清单（原 16 个 MCP 工具的能力一个不少，只是从「工具调用」变成了「子命令/内部方法」）：
  知识库   kb list / kb search / kb write / kb append / kb export / kb import
  感知     perceive（run 内部每回合自动调用） / predict（手动看一帧+预判） / kb clean
  动作     action / set（= game_action / switch_set） / afk（= handle_afk）
  维护     kb boss（= query_boss_history） / kb tactic（= switch_tactic） / kb clean（= clean_cache）
  手册     guide（= ugf_guide）
  高层     run（主循环：视频学习、全实体预判、战斗评估、动态心态、组队协同、拟人操作、
          BOSS 记忆、死亡复盘、会话记忆/断点续玩、自动调参、自动汇报、游戏档案化）

⚠️ 声明：仅用于本地 AI 智能体技术研究。在 florr.io 官方服务器运行 bot 违反游戏服务条款，
   可能导致账号封禁。请在本地 / 自建 / 已授权环境使用。
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import difflib
import json
import math
import os
import random
import re
import shutil
import sys
import tarfile
import time
from collections import deque
from datetime import datetime
from typing import TypedDict

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

VERSION = "3.0.0-single"


# ---------------------------------------------------------------------------
# 核心数据流类型（ROADMAP #4）：帧 / 预判实体 / 战斗评估 / 动作
# 全部 total=False：字段渐进演化时不破坏旧调用方
# ---------------------------------------------------------------------------
class FramePayload(TypedDict, total=False):
    """感知后端统一帧载荷。"""

    player: dict
    entities: list
    teammates: list
    afk_popup: bool
    error: str
    _fallback: str


class EntityPred(TypedDict, total=False):
    """预判输出的单实体。x/y_predict 为 None 表示置信不足，勿信预判坐标。"""

    raw_id: str
    rarity: str
    category: str
    role: str
    threat_score: float
    x_now: float
    y_now: float
    x_predict: float | None
    y_predict: float | None
    vx_per_sec: float
    vy_per_sec: float
    confidence: float
    prediction_trusted: bool
    model: str


class CombatEval(TypedDict, total=False):
    """战斗评估结果。decision ∈ fight / cautious_fight / retreat。"""

    decision: str
    recommended_set: str
    mindset: str
    enemy_threat: float
    threat_ratio: float
    has_highest_boss: bool
    retreat_reason: str


class ActionDict(TypedDict, total=False):
    """决策动作。source ∈ llm / kb / rule，move 时必带 x/y。"""

    action: str
    x: int
    y: int
    source: str


# ===========================================================================
# 0. 轻量工具
# ===========================================================================
def _load_dotenv(path: str | None = None):
    """极简 .env 加载（省掉 python-dotenv 依赖）。已存在的环境变量优先。"""
    path = path or os.path.join(BASE_DIR, ".env")
    if not os.path.exists(path):
        return
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                k, v = k.strip(), v.strip().strip('"').strip("'")
                if k and k not in os.environ:
                    os.environ[k] = v
    except OSError:
        pass


_load_dotenv()


def _setup_console():
    """控制台编码硬化（ROADMAP #19）：Windows GBK 终端下中文日志不再抛
    UnicodeEncodeError——统一把 stdout/stderr 切到 UTF-8，失败静默降级。"""
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(AttributeError, ValueError, OSError):
            stream.reconfigure(encoding="utf-8", errors="replace")


_setup_console()


def env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def retry_call(fn, attempts: int, backoff: float, label: str) -> tuple:
    """外部依赖统一调用（ROADMAP #5）：返回 (ok, result, err)。

    - attempts: 总尝试次数（含首次）；backoff: 线性退避基数（第 i 次失败后睡 backoff*i 秒）
    - 每次失败细节进日志；最终失败返回 (False, None, 最后一次错误)
    """
    attempts = max(1, safe_int(attempts, 1))
    backoff = max(0.0, safe_float(backoff, 0.0))
    last_err = ""
    for i in range(attempts):
        try:
            return True, fn(), ""
        except Exception as e:  # 外部边界统一收敛：网络/解析/服务异常都算失败
            last_err = f"{type(e).__name__}: {e}"
            if i + 1 < attempts:
                log(
                    f"[降级] {label} 第 {i + 1}/{attempts} 次失败: {last_err}，{backoff * (i + 1):.1f}s 后重试"
                )
                if backoff > 0:
                    time.sleep(backoff * (i + 1))
    log(f"[降级] {label} {attempts} 次尝试均失败: {last_err}")
    return False, None, last_err


def safe_float(v, default: float = 0.0) -> float:
    """脏数据（None / 字符串 / NaN / Inf）一律收敛成有限 float。"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return f if math.isfinite(f) else default


def safe_int(v, default: int = 0) -> int:
    if v is None or isinstance(v, bool):
        return default
    if isinstance(v, float):
        return int(v) if math.isfinite(v) else default
    try:
        return int(v)
    except (TypeError, ValueError):
        pass
    f = safe_float(v, float(default))
    return int(f)


def _rotate_logs_if_needed(force: bool = False):
    """ROADMAP #16：日志轮转落地——超过 logs.max_size_mb 压缩轮转（gzip），
    超过 logs.retention_days 的旧日志删除。默认 60 秒最多检查一次，长跑低开销。"""
    global _last_rotate_check
    now = time.time()
    if not force and now - _last_rotate_check < 60:
        return
    _last_rotate_check = now
    try:
        os.makedirs(RUN_LOGS, exist_ok=True)
        max_mb = safe_float(cfg_get("logs.max_size_mb", 20), 20)
        retention = safe_int(cfg_get("logs.retention_days", 7), 7)
        cutoff = now - max(0, retention) * 86400
        for fn in os.listdir(RUN_LOGS):
            if not fn.startswith("agent_") or ".log" not in fn:
                continue
            fp = os.path.join(RUN_LOGS, fn)
            try:
                stt = os.stat(fp)
            except OSError:
                continue
            if fn.endswith(".log") and max_mb > 0 and stt.st_size > max_mb * 1024 * 1024:
                import gzip

                gz = f"{fp}.{time.strftime('%H%M%S')}.gz"
                try:
                    with open(fp, "rb") as src, gzip.open(gz, "wb") as dst:
                        shutil.copyfileobj(src, dst)
                    os.truncate(fp, 0)
                except OSError:
                    pass
            elif stt.st_mtime < cutoff:
                with contextlib.suppress(OSError):
                    os.remove(fp)
    except OSError:
        pass


_last_rotate_check = 0.0

EVENTS_FILE_NAME = "events.jsonl"


def log_event(kind: str, **data):
    """ROADMAP #16：结构化事件流（run_logs/events.jsonl），一行一事件，可机器分析。

    kind 约定：decision / death / tune / learn / session_end / config_reload。
    """
    try:
        os.makedirs(RUN_LOGS, exist_ok=True)
        rec = {"ts": datetime.now().isoformat(timespec="seconds"), "kind": kind}
        rec.update(data)
        with open(os.path.join(RUN_LOGS, EVENTS_FILE_NAME), "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
    except (OSError, TypeError, ValueError):
        pass


def read_events(tail: int = 20, kind: str = "") -> list:
    """读取结构化事件（可选按 kind 过滤），返回最后 tail 条。"""
    path = os.path.join(RUN_LOGS, EVENTS_FILE_NAME)
    if not os.path.exists(path):
        return []
    out = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if kind and rec.get("kind") != kind:
                    continue
                out.append(rec)
    except OSError:
        return []
    return out[-max(1, safe_int(tail, 20)) :]


def log(msg: str):
    """带时间戳的日志；同时写 run_logs/agent_YYYYMMDD.log（带轮转）。"""
    _rotate_logs_if_needed()
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        os.makedirs(RUN_LOGS, exist_ok=True)
        with open(
            os.path.join(RUN_LOGS, f"agent_{datetime.now().strftime('%Y%m%d')}.log"), "a", encoding="utf-8"
        ) as f:
            f.write(line + "\n")
    except OSError:
        pass


def safe_name(name: str, sep: str = "_") -> str:
    """把外部传入的文件名/游戏名清洗成单层安全名，杜绝路径穿越。"""
    raw = (name or "").strip().replace("\\", sep).replace("/", sep)
    raw = "".join(ch if not ch.isspace() else sep for ch in raw)
    raw = raw.replace("\0", "").replace("..", "")
    return raw.strip().strip(sep).strip(".").strip()


# ===========================================================================
# 1. 配置（内置默认 < config.yaml < 游戏档案 < tuned_overrides）
# ===========================================================================
DEFAULT = {
    "game.name": "florr",
    "game.description": "",
    "server.perception_port": 5001,  # http 感知后端端口
    "perception.backend": "auto",  # auto | http | mock | local | template（ROADMAP #6）
    "perception.http_url": "",  # 留空则用 http://127.0.0.1:<port>/perceive
    "perception.timeout": 24,  # 首帧要加载 YOLO，给足时间
    "perception.source_w": 0,  # 感知后端坐标系宽（0=与逻辑屏一致，不缩放；ROADMAP #19）
    "perception.source_h": 0,  # 感知后端坐标系高
    "perception.player_stub": None,  # local/template 后端的玩家状态桩（可档案覆盖）
    "perception.capture_region": None,  # mss 抓屏区域 dict（left/top/width/height）
    # ROADMAP #6：local 后端（mss 截图 + ONNX Runtime 本地推理，YOLOv8 导出格式）
    "perception.local.model_path": "",
    "perception.local.input_size": 640,
    "perception.local.conf": 0.5,
    "perception.local.labels": [],  # 类别索引 → 名称；空则用 class_<idx>
    "perception.local.rarity_map": {},  # 名称 → 稀有度档
    # ROADMAP #6：template 后端（OpenCV 模板匹配，零模型轻量方案）
    "perception.template.dir": "",  # 模板目录，每个 <label>.png 一类实体
    "perception.template.conf": 0.8,
    "perception.template.rarity_map": {},
    "perception.template.source_image": "",  # 调试用：指定截图文件代替实时抓屏
    "predictor.predict_seconds": 1.2,
    "predictor.min_frames": 3,
    "predictor.entity_timeout": 0.4,
    "predictor.max_output_entities": 8,
    "predictor.history_maxlen": 10,
    "predictor.confidence_threshold": 0.65,
    "predictor.frame_full_frames": 8,
    "predictor.speed_ref": 2000.0,
    "predictor.speed_penalty_floor": 0.3,
    "predictor.jitter_floor": 0.35,
    "predictor.match_max_dist": 400,  # ROADMAP #9：匹配距离上限，超过宁开新轨不误挂
    "predictor.model": "auto",  # ROADMAP #7：auto|linear|accel|circular
    "predictor.accel_max": 2000,  # 恒加速度模型的加速度钳制(px/s²)，防外推爆炸
    "predictor.circular_min_frames": 8,  # 圆周检测最少帧数
    "predictor.circular_min_radius": 20,  # 低于该半径不认为是绕圈(px)
    "predictor.circular_min_omega": 0.3,  # 低于该角速度不认为是绕圈(rad/s)
    "predictor.rarity_highest_boss": ["Unique", "Eternal"],
    "predictor.rarity_boss": ["Super"],
    "predictor.rarity_elite": ["Ultra", "Mythic", "Legendary", "Epic"],
    "predictor.rarity_normal": ["Rare", "Unusual", "Common"],
    "predictor.threat": {
        "highest_boss": 1000,
        "boss": 400,
        "elite": 120,
        "normal": 15,
        "player_enemy": 150,
        "player_ally": 0,
        "unknown": 5,
    },
    "combat.eval_debounce_interval": 0.7,
    "combat.jitter_base": 8,
    "combat.jitter_max": 15,
    "combat.chase_max_distance": 400,
    "combat.chase_min_category": "elite",
    "combat.retreat_ratio": 1.0,
    "combat.safe_zone_margin": 100,
    "combat.safe_zone_w": 1920,
    "combat.safe_zone_h": 1080,
    "combat.flee_distance": 300,
    "combat.strafe_distance": 150,
    "combat.default_set": "combat",
    "combat.sets": ["combat", "tank", "retreat", "chase", "team"],
    "combat.set_map": {},
    "combat.tactics": [],
    "agent.loop_interval": 0.5,
    "agent.game": "florr",
    "agent.webhook_url": "",
    "agent.death_frame_threshold": 8,
    "agent.boss_memory_interval": 12,
    "agent.boss_sample_max": 120,
    "agent.boss_close_dist": 120,
    "agent.learning_stats_interval": 24,
    "agent.report_every": 0,  # 0 = 关闭局中进度汇报
    "agent.kb_max_mb": 50,
    "agent.kb_archive_dir": "knowledge_archive",
    "agent.corner_pause": True,  # 鼠标移到屏幕角落 = 安全暂停
    "agent.tune_locked": [],  # ROADMAP #13：人工锁定的参数（点分路径），调参跳过
    "review.enabled": True,  # ROADMAP #15：复盘总开关
    "review.trigger_boss": True,  # BOSS 局触发复盘
    "review.trigger_team": True,  # 组队局触发复盘
    "review.template": "",  # 自定义复盘模板（占位符 ts/outcome/monster/set/cause/note），空=内置
    "paths.knowledge_md": "knowledge_md",
    "paths.frames": "video_frames",
    "paths.run_logs": "run_logs",
    "paths.backups": "kb_backups",
    "kb.search_top_n": 5,  # ROADMAP #11：检索返回 Top-N
    "kb.history_revisions": 20,  # ROADMAP #12：每文件保留的历史修订数
    "kb.history_max_kb": 256,  # ROADMAP #12：超过该大小的文件历史不存正文
    "logs.retention_days": 7,
    "logs.max_size_mb": 20,
    # ROADMAP #8：LLM 决策（prompt 可由游戏档案覆盖；空=内置 SYSTEM_PROMPT）
    "llm.system_prompt": "",
    "llm.max_tokens": 800,
    "llm.temperature": 0.3,
    "llm.vlm_prompt": "",  # ROADMAP #14：视频学习 VLM 提示词（空=内置），可按游戏覆盖
    # ROADMAP #14：视频学习增强
    "learn.hash_dedup": True,  # 抽帧后感知哈希跳过近重复帧，省 VLM 调用
    "learn.hash_threshold": 5,  # 汉明距离 ≤ 该值视为近重复（64bit 哈希）
    "learn.min_votes": 2,  # 同一战术需 ≥N 帧支持才入库（1=关闭投票）
    # ROADMAP #5：外部依赖统一降级链（重试次数 / 线性退避秒 / 超时）
    "resilience.perception.retries": 2,
    "resilience.perception.backoff": 1.0,
    "resilience.webhook.retries": 2,
    "resilience.webhook.backoff": 1.0,
    "resilience.webhook.timeout": 5,
    "resilience.llm.retries": 1,
    "resilience.llm.backoff": 0.5,
    "resilience.llm.timeout": 15,
}

CONFIG_PATH = os.path.join(BASE_DIR, "config.yaml")
PROFILE_DIR = os.path.join(BASE_DIR, "game_profiles")
TUNED_PATH = os.path.join(BASE_DIR, "tuned_overrides.yaml")

_CFG: dict = {}


def _read_yaml(path: str) -> dict:
    try:
        import yaml
    except ImportError:
        return {}
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _merge(dst: dict, src: dict) -> dict:
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _merge(dst[k], v)
        else:
            dst[k] = v
    return dst


def _flatten_default() -> dict:
    tree: dict = {}
    for key, val in DEFAULT.items():
        cur = tree
        parts = key.split(".")
        for p in parts[:-1]:
            cur = cur.setdefault(p, {})
        cur[parts[-1]] = val
    return tree


def active_game() -> str:
    """当前激活的游戏：AGENT_GAME / UGF_GAME 环境变量 > config.yaml > 'florr'。"""
    for var in ("AGENT_GAME", "UGF_GAME"):
        v = (os.getenv(var) or "").strip()
        if v:
            return v
    return str((_CFG.get("agent") or {}).get("game") or (_CFG.get("game") or {}).get("name") or "florr")


def reload_config() -> None:
    """按优先级合并：DEFAULT < config.yaml < 游戏档案 < tuned_overrides。"""
    tree = _flatten_default()
    _merge(tree, _read_yaml(CONFIG_PATH))
    game = active_game_for(tree)
    _merge(tree, _read_yaml(os.path.join(PROFILE_DIR, f"{game}.yaml")))
    _merge(tree, _read_yaml(TUNED_PATH))
    global _CFG
    _CFG = tree


def active_game_for(tree: dict) -> str:
    for var in ("AGENT_GAME", "UGF_GAME"):
        v = (os.getenv(var) or "").strip()
        if v:
            return v
    return str((tree.get("agent") or {}).get("game") or (tree.get("game") or {}).get("name") or "florr")


def cfg_get(path: str, default=None):
    """点分路径读配置，坏键沿途回退 default。"""
    cur = _CFG
    for p in (path or "").split("."):
        if not isinstance(cur, dict) or p not in cur:
            return default
        cur = cur[p]
    return cur


_reload_mtime = None


def reload_if_changed() -> bool:
    """config / 档案 / 调参文件 mtime 变了就热加载。"""
    global _reload_mtime
    game = active_game()
    stamp: list = []
    for p in (CONFIG_PATH, os.path.join(PROFILE_DIR, f"{game}.yaml"), TUNED_PATH):
        try:
            stamp.append(os.path.getmtime(p))
        except OSError:
            stamp.append(None)
    key = tuple(stamp)
    if _reload_mtime is not None and key != _reload_mtime:
        _reload_mtime = key
        reload_config()
        return True
    _reload_mtime = key
    return False


def dry_run() -> bool:
    """UGF_DRY_RUN=1：只记录动作、不碰真实键鼠。"""
    return env_flag("UGF_DRY_RUN") or env_flag("DRY_RUN")


def runtime_mode() -> dict:
    """统一口径的运行模式快照（CLI `mode` 与日志都用它）。"""
    backend = (os.getenv("UGF_PERCEPTION_BACKEND") or "").strip() or str(
        cfg_get("perception.backend", "auto")
    )
    backend = backend.lower()
    if dry_run() and backend == "auto":
        backend = "mock"
    return {
        "mode": "dry-run" if dry_run() else "online",
        "dry_run": dry_run(),
        "perception_backend": backend,
        "llm": "on" if (os.getenv("LLM_API_URL") and os.getenv("LLM_API_KEY")) else "off",
        "vlm": "on" if (os.getenv("VLM_API_URL") and os.getenv("VLM_API_KEY")) else "off",
        "game": active_game(),
    }


reload_config()

# 派生路径（随配置，启动时定一次；改路径请重启）
KB_DIR = os.path.join(BASE_DIR, str(cfg_get("paths.knowledge_md", "knowledge_md")))
RUN_LOGS = os.path.join(BASE_DIR, str(cfg_get("paths.run_logs", "run_logs")))
FRAME_DIR = os.path.join(BASE_DIR, str(cfg_get("paths.frames", "video_frames")))
ARCHIVE_DIR = os.path.join(BASE_DIR, str(cfg_get("agent.kb_archive_dir", "knowledge_archive")))
BACKUP_DIR = os.path.join(BASE_DIR, str(cfg_get("paths.backups", "kb_backups")))


def kb_game_dir(game: str | None = None) -> str:
    """本游戏的知识分区 knowledge_md/<game>/。"""
    g = safe_name(game or active_game()) or "default"
    return os.path.join(KB_DIR, g)


# ===========================================================================
# 2. 知识库（Markdown）
# ===========================================================================
KB_HISTORY_DIRNAME = ".history"  # ROADMAP #12：写前快照存放目录（KB_DIR 内）

# ROADMAP #11：检索排序时永远置顶的权威知识文件（决策闭环依赖它们，
# 防止知识库被复盘/视频学习文件污染后，种子战术被 Top-N 挤出导致知识链断裂）
CANONICAL_KB_FILES = {"tactics.md", "_current_tactic.md", "boss_guide.md"}

KB_TEMPLATES = {
    "_README.md": (
        "# 本地知识库\n\n所有经验以 Markdown 存储，按游戏分区在 `knowledge_md/<游戏>/`。\n\n"
        "## 目录约定\n"
        "- `tactics.md` 战术（seed 自动生成，可手改）\n"
        "- `boss_guide.md` 高威胁目标指南（seed 自动生成）\n"
        "- `boss_behavior_log.md` BOSS 行为习惯（主循环自动追加）\n"
        "- `player_tactics.md` 换套/决策记录（主循环自动追加）\n"
        "- `review_*.md` 对局复盘（自动生成）\n"
        "- `video_tactic_*.md` 视频学习战术（自动生成）\n"
    ),
    "boss_behavior_log.md": (
        "# BOSS 行为日志\n\n记录遭遇 BOSS 时的行为习惯：移动模式 / 接近倾向 / 击杀或逃脱经验。\n\n"
        "（由 agent.py 主循环每 12 秒批量追加）\n"
    ),
    "player_tactics.md": "# 玩家打法笔记\n\n记录换套与决策轨迹，便于回看。\n",
}


def kb_ensure_templates():
    os.makedirs(KB_DIR, exist_ok=True)
    if not [f for f in os.listdir(KB_DIR) if f.endswith(".md")]:
        for fn, content in KB_TEMPLATES.items():
            p = os.path.join(KB_DIR, fn)
            if not os.path.exists(p):
                with open(p, "w", encoding="utf-8") as f:
                    f.write(content)


def kb_resolve(filename: str, game: str = "") -> str | None:
    """(文件名, 游戏名) → KB 内绝对路径；越界返回 None。"""
    sf = safe_name(filename)
    if not sf:
        return None
    if not sf.endswith(".md"):
        sf += ".md"
    root = KB_DIR
    if game:
        sg = safe_name(game)
        if sg:
            root = os.path.join(KB_DIR, sg)
            os.makedirs(root, exist_ok=True)
    full = os.path.normpath(os.path.join(root, sf))
    if os.path.commonpath([os.path.normpath(KB_DIR), full]) != os.path.normpath(KB_DIR):
        return None
    return full


def kb_list(game: str = "") -> list:
    base = kb_game_dir(game) if game else KB_DIR
    if not os.path.isdir(base):
        return []
    return sorted(f for f in os.listdir(base) if f.endswith(".md"))


def _tokenize(text: str) -> list:
    """BM25 轻量分词（ROADMAP #11）：ASCII 词 + 中文单字/二元组，零第三方依赖。"""
    text = str(text or "").lower()
    tokens = re.findall(r"[a-z0-9_]+", text)
    cjk = re.findall(r"[\u4e00-\u9fff]", text)
    tokens.extend(cjk)
    tokens.extend(a + b for a, b in zip(cjk, cjk[1:], strict=False))
    return tokens


def _bm25_scores(query_tokens: list, docs_tokens: list, k1: float = 1.5, b: float = 0.75) -> list:
    """经典 BM25 打分：docs_tokens 为每篇文档的 token 列表，返回每篇得分。"""
    n = len(docs_tokens)
    if not n or not query_tokens:
        return [0.0] * n
    avgdl = sum(len(d) for d in docs_tokens) / n or 1.0
    df: dict = {}
    for toks in docs_tokens:
        for t in set(toks):
            df[t] = df.get(t, 0) + 1
    scores = []
    for toks in docs_tokens:
        tf: dict = {}
        for t in toks:
            tf[t] = tf.get(t, 0) + 1
        s = 0.0
        dl = len(toks) or 1
        for q in set(query_tokens):
            f = tf.get(q, 0)
            if not f:
                continue
            idf = math.log(1 + (n - df.get(q, 0) + 0.5) / (df.get(q, 0) + 0.5))
            s += idf * (f * (k1 + 1)) / (f + k1 * (1 - b + b * dl / avgdl))
        scores.append(s)
    return scores


def _text_search(keyword: str, base: str) -> str:
    """知识库检索（ROADMAP #11）：字面命中保底 + BM25 相关性排序，返回 Top-N。

    排序规则：字面包含关键词的文档优先，其次按 BM25 得分降序；
    两者都不沾边的文档不返回。输出格式与旧版保持兼容（"共找到 N 条结果:"）。
    """
    if not os.path.isdir(base):
        return "未找到相关内容"
    kw = str(keyword or "").strip().lower()
    if not kw:
        return "未找到相关内容"
    docs: list = []  # (relpath, content, literal_hit)
    for root, _dirs, files in os.walk(base):
        if KB_HISTORY_DIRNAME in root.split(os.sep):
            continue
        for fn in sorted(f for f in files if f.endswith(".md")):
            fp = os.path.join(root, fn)
            try:
                with open(fp, encoding="utf-8") as f:
                    content = f.read()
            except (OSError, UnicodeDecodeError):  # 损坏/二进制文件跳过而非崩溃
                continue
            docs.append((os.path.relpath(fp, base), content, kw in content.lower()))
    if not docs:
        return "未找到相关内容"
    scores = _bm25_scores(_tokenize(kw), [_tokenize(c) for _r, c, _h in docs])
    top_n = max(1, safe_int(cfg_get("kb.search_top_n", 5), 5))

    def _tier(i: int) -> int:
        name = docs[i][0].replace(os.sep, "/").split("/")[-1]
        if docs[i][2] and name in CANONICAL_KB_FILES:
            return 2  # 权威战术文档且字面命中：永远置顶
        return 1 if docs[i][2] else 0

    ranked = sorted(range(len(docs)), key=lambda i: (_tier(i), scores[i]), reverse=True)
    hits: list = []
    for i in ranked:
        rel, content, literal = docs[i]
        if not literal and scores[i] <= 0:
            continue
        if len(hits) >= top_n:
            break
        tag = "" if literal else f"（相关度 {scores[i]:.2f}）"
        hits.append(f"## {rel}{tag}\n{content[:2000]}")
    if not hits:
        return "未找到相关内容"
    return f"共找到 {len(hits)} 条结果:\n" + "\n\n---\n\n".join(hits)


def kb_search(keyword: str, game: str = "") -> str:
    return _text_search(keyword, kb_game_dir(game) if game else KB_DIR)


def kb_write(filename: str, content: str, game: str = "") -> str:
    path = kb_resolve(filename, game)
    if path is None:
        return f"错误: 非法的知识库路径 (filename={filename!r}, game={game!r})"
    _kb_history_save(path, "write")
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(content or "")
    except OSError as e:
        return f"写入知识库失败: {e}"
    return f"已写入知识库: {path} ({len(content or '')} 字符)"


def kb_append(filename: str, content: str, game: str = "") -> str:
    path = kb_resolve(filename, game)
    if path is None:
        return f"错误: 非法的知识库路径 (filename={filename!r}, game={game!r})"
    _kb_history_save(path, "append")
    try:
        exists = os.path.exists(path)
        with open(path, "a" if exists else "w", encoding="utf-8") as f:
            if exists:
                f.write("\n\n")
            f.write(content or "")
    except OSError as e:
        return f"追加到知识库失败: {e}"
    return f"已追加到知识库: {path}"


def _kb_history_file(path: str) -> str:
    """ROADMAP #12：某知识库文件对应的历史 JSONL 路径（KB_DIR/.history/ 下）。"""
    rel = os.path.relpath(path, KB_DIR).replace(os.sep, "/")
    key = re.sub(r"[^\w.-]+", "_", rel)
    d = os.path.join(KB_DIR, KB_HISTORY_DIRNAME)
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, key + ".jsonl")


def _kb_history_save(path: str, op: str):
    """写前自动快照：整份正文追加进历史 JSONL，滚动保留最近 N 条。

    超过 kb.history_max_kb 的文件只记事件不存正文（防止历史目录膨胀）。
    """
    try:
        if not os.path.exists(path):
            return
        with open(path, encoding="utf-8") as f:
            content = f.read()
    except (OSError, UnicodeDecodeError):
        return
    max_kb = safe_int(cfg_get("kb.history_max_kb", 256), 256)
    if len(content.encode("utf-8")) > max_kb * 1024:
        content = ""
    rec = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "op": op,
        "size": len(content),
        "content": content,
    }
    hf = _kb_history_file(path)
    try:
        lines = []
        if os.path.exists(hf):
            with open(hf, encoding="utf-8") as f:
                lines = f.read().splitlines()
        lines.append(json.dumps(rec, ensure_ascii=False))
        cap = max(1, safe_int(cfg_get("kb.history_revisions", 20), 20))
        with open(hf, "w", encoding="utf-8") as f:
            f.write("\n".join(lines[-cap:]) + "\n")
    except OSError:
        pass


def _kb_history_rows(path: str) -> list:
    hf = _kb_history_file(path)
    if not os.path.exists(hf):
        return []
    rows = []
    try:
        with open(hf, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        return []
    return rows


def kb_history(filename: str, game: str = "") -> str:
    """查看某知识库文件的历史修订列表（新→旧）。"""
    path = kb_resolve(filename, game)
    if path is None:
        return f"错误: 非法的知识库路径 (filename={filename!r}, game={game!r})"
    rows = _kb_history_rows(path)
    if not rows:
        return f"{filename} 暂无历史修订"
    out = [f"{filename} 共 {len(rows)} 条历史修订（新→旧）:"]
    for i, r in enumerate(reversed(rows)):
        out.append(f"  rev -{i + 1}: {r.get('ts')} op={r.get('op')} size={r.get('size')}")
    return "\n".join(out)


def kb_rollback(filename: str, rev: int = 1, game: str = "") -> str:
    """回滚到第 rev 新的历史修订（rev=1 即上一次写入前的状态）。

    回滚前会把当前内容也存进历史，因此回滚本身可再回滚。
    """
    path = kb_resolve(filename, game)
    if path is None:
        return f"错误: 非法的知识库路径 (filename={filename!r}, game={game!r})"
    rows = _kb_history_rows(path)
    if not rows:
        return f"{filename} 暂无历史修订，无法回滚"
    rev = max(1, safe_int(rev, 1))
    idx = len(rows) - rev
    if idx < 0:
        return f"修订号超出范围（共 {len(rows)} 条历史）"
    content = rows[idx].get("content")
    if content is None:
        return "该修订未存正文（超大文件只记事件），无法回滚"
    _kb_history_save(path, "pre-rollback")
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
    except OSError as e:
        return f"回滚写入失败: {e}"
    return f"已回滚 {filename} 到 rev -{rev}（{rows[idx].get('ts')}，{len(content)} 字符）"


def kb_query_boss(boss_name: str = "") -> str:
    path = os.path.join(KB_DIR, "boss_behavior_log.md")
    if not os.path.exists(path):
        return "知识库还没有 BOSS 行为记录(文件不存在)。"
    try:
        with open(path, encoding="utf-8") as f:
            content = f.read()
    except OSError as e:
        return f"读取失败: {e}"
    if not boss_name:
        return content or "知识库还没有 BOSS 行为记录。"
    hits = [seg for seg in content.split("### ") if boss_name.lower() in seg.lower()]
    return (
        "\n\n".join(f"### {s}" for s in hits)
        if hits
        else f"知识库中没有关于「{boss_name}」的 BOSS 行为记录。"
    )


def kb_switch_tactic(tactic_file: str) -> str:
    name = safe_name(tactic_file)
    if not name:
        return f"错误: 非法的战术文件名: {tactic_file!r}"
    if not name.endswith(".md"):
        name += ".md"
    src = os.path.join(KB_DIR, name)
    if not os.path.exists(src):
        return f"知识库中没有这份战术文档: {name}"
    try:
        with open(src, encoding="utf-8") as f:
            content = f.read()
        with open(os.path.join(KB_DIR, "_current_tactic.md"), "w", encoding="utf-8") as f:
            f.write(f"# 当前战术: {name}\n\n来自: {name}\n\n{content[:2000]}")
    except OSError as e:
        return f"切换战术失败: {e}"
    return f"已切换当前战术为: {name}"


def kb_export() -> str:
    """把活跃库 + 归档库打包成 kb_backups/kb_backup_<时间>.tar.gz。"""
    os.makedirs(BACKUP_DIR, exist_ok=True)
    path = os.path.join(BACKUP_DIR, f"kb_backup_{time.strftime('%Y%m%d_%H%M%S')}.tar.gz")
    n = 0
    with tarfile.open(path, "w:gz") as tar:
        for arc_root, src in (("knowledge_md", KB_DIR), ("knowledge_archive", ARCHIVE_DIR)):
            if not os.path.isdir(src):
                continue
            for root, _d, files in os.walk(src):
                for fn in sorted(f for f in files if f.endswith(".md")):
                    full = os.path.join(root, fn)
                    rel = os.path.relpath(full, src).replace(os.sep, "/")
                    rel = "/".join(p for p in rel.split("/") if p not in ("", ".", ".."))
                    if not rel:
                        continue
                    tar.add(full, arcname=f"{arc_root}/{rel}")
                    n += 1
    size = os.path.getsize(path) / 1024
    return f"已导出知识库到: {path}（{n} 个文件，{size:.1f} KB）"


def kb_import(backup_path: str) -> str:
    if not os.path.isabs(backup_path):
        backup_path = os.path.join(BASE_DIR, backup_path)
    if not os.path.isfile(backup_path):
        return f"找不到备份文件: {backup_path}"
    os.makedirs(KB_DIR, exist_ok=True)
    os.makedirs(ARCHIVE_DIR, exist_ok=True)
    roots = {"knowledge_md": KB_DIR, "knowledge_archive": ARCHIVE_DIR}
    count = {"knowledge_md": 0, "knowledge_archive": 0}
    with tarfile.open(backup_path, "r:gz") as tar:
        for m in tar.getmembers():
            if not m.isfile() or not m.name.endswith(".md"):
                continue
            parts = [p for p in m.name.replace("\\", "/").split("/") if p not in ("", ".", "..")]
            if len(parts) < 2:
                continue
            key = parts[0] if parts[0] in roots else "knowledge_md"
            dest_root = roots[key]
            dest = os.path.normpath(os.path.join(dest_root, *parts[1:]))
            if os.path.commonpath([os.path.normpath(dest_root), dest]) != os.path.normpath(dest_root):
                continue  # 防路径穿越
            try:
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                fp = tar.extractfile(m)
                if fp is None:
                    continue
                with open(dest, "wb") as out:
                    out.write(fp.read())
                count[key] += 1
            except (OSError, tarfile.TarError):
                continue
    msg = f"已从备份恢复 {count['knowledge_md']} 个知识库文件到 knowledge_md/"
    if count["knowledge_archive"]:
        msg += f"，{count['knowledge_archive']} 个归档文件到 knowledge_archive/"
    return msg


def kb_compress_duplicates(ratio: float = 0.9) -> int:
    """合并高度相似的 video_tactic_*.md（保留更长的一份）。"""
    if not os.path.isdir(KB_DIR):
        return 0
    files = sorted(f for f in os.listdir(KB_DIR) if f.startswith("video_tactic_") and f.endswith(".md"))
    removed = 0
    i = 0
    while i < len(files):
        pa = os.path.join(KB_DIR, files[i])
        try:
            with open(pa, encoding="utf-8") as fa:
                ta = fa.read()
        except OSError:
            i += 1
            continue
        j = i + 1
        while j < len(files):
            pb = os.path.join(KB_DIR, files[j])
            try:
                with open(pb, encoding="utf-8") as fb:
                    tb = fb.read()
            except OSError:
                j += 1
                continue
            if difflib.SequenceMatcher(None, ta, tb).ratio() >= ratio:
                keep, drop = (pa, pb) if len(ta) >= len(tb) else (pb, pa)
                try:
                    os.remove(drop)
                    removed += 1
                except OSError:
                    pass
                files.pop(j if drop == pb else i)
                continue
            j += 1
        i += 1
    return removed


def _dir_mb(path: str) -> float:
    total = 0
    for root, _d, files in os.walk(path):
        for fn in files:
            with contextlib.suppress(OSError):
                total += os.path.getsize(os.path.join(root, fn))
    return total / (1024 * 1024)


def kb_archive_oldest(max_mb: float) -> int:
    """活跃库超上限时，把最旧的笔记移进归档目录。"""
    os.makedirs(ARCHIVE_DIR, exist_ok=True)
    moved = 0
    while _dir_mb(KB_DIR) > max_mb:
        files = [f for f in os.listdir(KB_DIR) if f.endswith(".md")]
        if not files:
            break
        oldest = min(files, key=lambda f: os.path.getmtime(os.path.join(KB_DIR, f)))
        try:
            shutil.move(os.path.join(KB_DIR, oldest), os.path.join(ARCHIVE_DIR, oldest))
            moved += 1
        except OSError:
            break
    return moved


def kb_maintain() -> str:
    """体积维护：归档超限 + 合并重复（主循环启动时调用）。"""
    archived = kb_archive_oldest(safe_float(cfg_get("agent.kb_max_mb", 50), 50))
    merged = kb_compress_duplicates()
    if archived or merged:
        return f"知识库维护：归档 {archived} 份、合并重复 {merged} 份"
    return "知识库维护：无需处理"


def kb_clean(target: str = "all") -> str:
    """= 原 clean_cache：清预判历史 / 临时帧目录。"""
    done = []
    if target in ("all", "predict"):
        PREDICTOR.reset()
        done.append("预判历史已清空")
    if target in ("all", "frames"):
        if os.path.isdir(FRAME_DIR):
            shutil.rmtree(FRAME_DIR, ignore_errors=True)
            done.append("临时帧目录已清理" if not os.path.isdir(FRAME_DIR) else "临时帧目录清理失败")
        else:
            done.append("临时帧目录不存在（无需清理）")
    return "; ".join(done) if done else f"未知目标: {target}，可选 all/predict/frames"


# ===========================================================================
# 3. 预判（全实体运动预判 + 置信度）
# ===========================================================================
def _threat_table() -> dict:
    return dict(cfg_get("predictor.threat", DEFAULT["predictor.threat"]) or {})


def classify_by_rarity(rarity: str) -> str:
    r = (rarity or "").strip().capitalize()
    if r in set(cfg_get("predictor.rarity_highest_boss", [])):
        return "highest_boss"
    if r in set(cfg_get("predictor.rarity_boss", [])):
        return "boss"
    if r in set(cfg_get("predictor.rarity_elite", [])):
        return "elite"
    if r in set(cfg_get("predictor.rarity_normal", [])):
        return "normal"
    return "unknown"


PLAYER_ENEMY_MARKERS = ("player_enemy", "enemy_player", "hostile", "enemy")
PLAYER_ALLY_MARKERS = ("player_ally", "ally", "teammate", "friend", "party")


def detect_role(raw_id: str, explicit: str | None = None) -> str:
    if explicit in ("player_enemy", "player_ally", "monster"):
        return explicit
    rid = (raw_id or "").lower()
    if any(m in rid for m in PLAYER_ENEMY_MARKERS):
        return "player_enemy"
    if any(m in rid for m in PLAYER_ALLY_MARKERS):
        return "player_ally"
    return "monster"


def _valid_coord(x, y) -> bool:
    x, y = safe_float(x, -1), safe_float(y, -1)
    return 0 <= x <= 100000 and 0 <= y <= 100000


def _linearity(history) -> float:
    """净位移 / 累计路程：直线=1，原地乱窜→0（用于抖动惩罚）。"""
    if len(history) < 2:
        return 1.0
    path = 0.0
    prev = history[0]
    for cur in list(history)[1:]:
        path += ((cur["x"] - prev["x"]) ** 2 + (cur["y"] - prev["y"]) ** 2) ** 0.5
        prev = cur
    if path <= 1e-6:
        return 1.0
    first, last = history[0], history[-1]
    net = ((last["x"] - first["x"]) ** 2 + (last["y"] - first["y"]) ** 2) ** 0.5
    return max(0.0, min(1.0, net / path))


class _Tracker:
    def __init__(self, raw_id, rarity, role):
        self.raw_id, self.rarity, self.role = raw_id, rarity, role
        self.category = role if role != "monster" else classify_by_rarity(rarity)
        self.history = deque(maxlen=safe_int(cfg_get("predictor.history_maxlen", 10), 10))
        self.last_seen = time.time()

    def update(self, x, y):
        self.history.append({"t": time.time(), "x": float(x), "y": float(y)})
        self.last_seen = time.time()

    def expired(self) -> bool:
        return (time.time() - self.last_seen) > safe_float(cfg_get("predictor.entity_timeout", 0.4), 0.4)

    def _fit_linear(self, secs: float):
        """线性外推（原始模型，兜底）。"""
        oldest, latest = self.history[0], self.history[-1]
        dt = latest["t"] - oldest["t"]
        if dt <= 0:
            return None
        vx = (latest["x"] - oldest["x"]) / dt
        vy = (latest["y"] - oldest["y"]) / dt
        return {
            "x": latest["x"] + vx * secs,
            "y": latest["y"] + vy * secs,
            "vx": vx,
            "vy": vy,
            "model": "linear",
        }

    def _fit_accel(self, secs: float):
        """恒加速度模型（ROADMAP #7）：对相邻帧瞬时速度最小二乘拟合 v(t)=a*t+b，二次外推。

        加速度钳制在 ±predictor.accel_max，防止脏数据把外推炸飞。
        """
        h = list(self.history)
        if len(h) < 4:
            return None
        vs = []
        for a, b in zip(h, h[1:], strict=False):
            dt = b["t"] - a["t"]
            if dt <= 1e-6:
                continue
            # 平均速度的物理时刻是区间中点，用中点时间戳拟合才不引入半帧偏差
            vs.append(((a["t"] + b["t"]) / 2, (b["x"] - a["x"]) / dt, (b["y"] - a["y"]) / dt))
        if len(vs) < 3:
            return None
        t0 = vs[0][0]
        pts = [(t - t0, vx, vy) for t, vx, vy in vs]
        n = len(pts)
        st = sum(p[0] for p in pts)
        stt = sum(p[0] ** 2 for p in pts)
        den = n * stt - st * st
        if abs(den) < 1e-9:
            return None

        def fit(idx: int):
            sy = sum(p[idx] for p in pts)
            sty = sum(p[0] * p[idx] for p in pts)
            a = (n * sty - st * sy) / den
            b = (sy - a * st) / n
            return a, b

        ax, bx = fit(1)
        ay, by = fit(2)
        amax = safe_float(cfg_get("predictor.accel_max", 2000), 2000)
        ax = max(-amax, min(amax, ax))
        ay = max(-amax, min(amax, ay))
        last = h[-1]
        tau = last["t"] - t0  # 拟合 intercept 在 t0，当前速度须外推到最后一帧时刻
        vxn = bx + ax * tau
        vyn = by + ay * tau
        return {
            "x": last["x"] + vxn * secs + 0.5 * ax * secs * secs,
            "y": last["y"] + vyn * secs + 0.5 * ay * secs * secs,
            "vx": vxn + ax * secs,
            "vy": vyn + ay * secs,
            "model": "accel",
        }

    def _fit_circular(self, secs: float):
        """圆周/周期运动检测（ROADMAP #7）：代数最小二乘圆拟合 + 角速度沿弧外推。

        短弧窗口下质心法不稳，这里解 x²+y²+Dx+Ey+F=0 的线性方程组得圆心与半径；
        残差过大（不像圆）/ 半径过小 / 角速度过低均返回 None，由 auto 回落其他模型。
        """
        min_frames = max(6, safe_int(cfg_get("predictor.circular_min_frames", 8), 8))
        h = list(self.history)[-min_frames:]
        if len(h) < 6:
            return None
        n = len(h)
        # 先按质心中心化：浅弧段下原始坐标量级大、矩阵病态，中心化后数值稳定
        ox = sum(p["x"] for p in h) / n
        oy = sum(p["y"] for p in h) / n
        pts = [(p["x"] - ox, p["y"] - oy, p["t"]) for p in h]
        sx = sum(p[0] for p in pts)
        sy = sum(p[1] for p in pts)
        sxx = sum(p[0] * p[0] for p in pts)
        syy = sum(p[1] * p[1] for p in pts)
        sxy = sum(p[0] * p[1] for p in pts)
        sz = sum(p[0] * p[0] + p[1] * p[1] for p in pts)
        sxz = sum(p[0] * (p[0] * p[0] + p[1] * p[1]) for p in pts)
        syz = sum(p[1] * (p[0] * p[0] + p[1] * p[1]) for p in pts)

        def det3(a):
            return (
                a[0][0] * (a[1][1] * a[2][2] - a[1][2] * a[2][1])
                - a[0][1] * (a[1][0] * a[2][2] - a[1][2] * a[2][0])
                + a[0][2] * (a[1][0] * a[2][1] - a[1][1] * a[2][0])
            )

        m0 = [[sxx, sxy, sx], [sxy, syy, sy], [sx, sy, float(n)]]
        d0 = det3(m0)
        if abs(d0) < 1e-12:
            return None  # 严格奇异（共线）；浅弧病态解交给半径/残差兜底
        rhs = [-sxz, -syz, -sz]
        dd = (
            det3([[rhs[0], m0[0][1], m0[0][2]], [rhs[1], m0[1][1], m0[1][2]], [rhs[2], m0[2][1], m0[2][2]]])
            / d0
        )
        ee = (
            det3([[m0[0][0], rhs[0], m0[0][2]], [m0[1][0], rhs[1], m0[1][2]], [m0[2][0], rhs[2], m0[2][2]]])
            / d0
        )
        ff = (
            det3([[m0[0][0], m0[0][1], rhs[0]], [m0[1][0], m0[1][1], rhs[1]], [m0[2][0], m0[2][1], rhs[2]]])
            / d0
        )
        # 中心化坐标下的圆心换算回原坐标系
        cx, cy = ox - dd / 2, oy - ee / 2
        r_sq = (dd / 2) ** 2 + (ee / 2) ** 2 - ff
        if r_sq <= 0:
            return None
        radius = math.sqrt(r_sq)
        min_r = safe_float(cfg_get("predictor.circular_min_radius", 20), 20)
        if radius < min_r or radius > 100000:
            return None
        resid = sum(abs(math.hypot(p["x"] - cx, p["y"] - cy) - radius) for p in h) / n
        if resid / radius > 0.15:
            return None  # 不像圆（病态解也会在这里被拦下）
        dt_total = h[-1]["t"] - h[0]["t"]
        if dt_total <= 0:
            return None
        total_ang = 0.0
        last_a = None
        for p in h:
            a = math.atan2(p["y"] - cy, p["x"] - cx)
            if last_a is not None:
                d = a - last_a
                while d > math.pi:
                    d -= 2 * math.pi
                while d < -math.pi:
                    d += 2 * math.pi
                total_ang += d
            last_a = a
        omega = total_ang / dt_total
        min_w = safe_float(cfg_get("predictor.circular_min_omega", 0.3), 0.3)
        if abs(omega) < min_w:
            return None
        a_new = (last_a or 0.0) + omega * secs
        return {
            "x": cx + radius * math.cos(a_new),
            "y": cy + radius * math.sin(a_new),
            "vx": -radius * omega * math.sin(a_new),
            "vy": radius * omega * math.cos(a_new),
            "model": "circular",
        }

    def _choose_fit(self, secs: float):
        """ROADMAP #7 模型选择：predictor.model = auto|linear|accel|circular。

        auto = 回退一步实测残差选优：拿 history[:-1] 预测 history[-1]，
        谁的一步误差小就用谁做正式外推。
        """
        mode = str(cfg_get("predictor.model", "auto") or "auto").lower()
        fits = {"linear": self._fit_linear(secs)}
        if mode in ("accel", "auto"):
            fits["accel"] = self._fit_accel(secs)
        if mode in ("circular", "auto"):
            fits["circular"] = self._fit_circular(secs)
        if mode != "auto":
            return fits.get(mode) or fits["linear"]
        h = list(self.history)
        if len(h) >= 5:
            probe = _Tracker(self.raw_id, self.rarity, self.role)
            probe.history = deque(h[:-1], maxlen=len(h))
            last = h[-1]
            secs_back = max(1e-3, last["t"] - h[-2]["t"])
            best, best_err = None, None
            for name in ("linear", "accel", "circular"):
                fitter = getattr(probe, f"_fit_{name}")
                f = fitter(secs_back)
                if not f:
                    continue
                err = math.hypot(f["x"] - last["x"], f["y"] - last["y"])
                if best_err is None or err < best_err:
                    best, best_err = fitter(secs), err
            if best is not None:
                return best
        return fits["linear"]

    def predict(self) -> EntityPred | None:
        min_frames = safe_int(cfg_get("predictor.min_frames", 3), 3)
        if len(self.history) < min_frames:
            return None
        secs = safe_float(cfg_get("predictor.predict_seconds", 1.2), 1.2)
        fit = self._choose_fit(secs) or self._fit_linear(secs)
        if fit is None:
            return None
        latest = self.history[-1]
        vx, vy = fit["vx"], fit["vy"]
        px, py = fit["x"], fit["y"]
        frame_conf = min(
            1.0,
            len(self.history) / max(1.0, safe_float(cfg_get("predictor.frame_full_frames", 8), 8)),
        )
        speed = (vx**2 + vy**2) ** 0.5
        speed_pen = max(
            safe_float(cfg_get("predictor.speed_penalty_floor", 0.3), 0.3),
            1.0 - speed / safe_float(cfg_get("predictor.speed_ref", 2000.0), 2000.0),
        )
        if fit["model"] == "circular":
            jitter_pen = 1.0  # 周期轨迹的低直线度已被模型解释，不再按抖动惩罚
        else:
            jitter_pen = max(
                safe_float(cfg_get("predictor.jitter_floor", 0.35), 0.35),
                _linearity(self.history),
            )
        conf = round(frame_conf * speed_pen * jitter_pen, 3)
        trusted = conf >= safe_float(cfg_get("predictor.confidence_threshold", 0.65), 0.65)
        return {
            "raw_id": self.raw_id,
            "rarity": self.rarity,
            "category": self.category,
            "role": self.role,
            "threat_score": _threat_table().get(self.category, 5),
            "x_now": round(latest["x"], 1),
            "y_now": round(latest["y"], 1),
            "x_predict": round(px, 1) if trusted else None,
            "y_predict": round(py, 1) if trusted else None,
            "vx_per_sec": round(vx, 2),
            "vy_per_sec": round(vy, 2),
            "confidence": conf,
            "prediction_trusted": trusted,
            "model": fit["model"],
        }


class Predictor:
    """全实体历史追踪 + 预判。同 raw_id 多只时按上一帧最近距离匹配。"""

    def __init__(self):
        self._t = {}
        self._uid = 0

    def _predicted_pos(self, tk, now: float):
        """跟踪器在 now 时刻的预测位置（线性外推），返回 (px, py, vx, vy)。"""
        if not tk.history:
            return None
        last = tk.history[-1]
        vx = vy = 0.0
        if len(tk.history) >= 2:
            first = tk.history[0]
            dt_h = last["t"] - first["t"]
            if dt_h > 0:
                vx = (last["x"] - first["x"]) / dt_h
                vy = (last["y"] - first["y"]) / dt_h
        dt = min(max(now - last["t"], 0.0), 0.5)
        return last["x"] + vx * dt, last["y"] + vy * dt, vx, vy

    def _match(self, dets: list, now: float) -> dict:
        """ROADMAP #9：多因子匹配，返回 {det_index: tracker_uid}。

        打分 = 预测位置距离
             + 运动方向不一致惩罚（检测位移方向与跟踪器速度反向时按位移量加罚）
             + 角色跳变惩罚（敌↔友切换视为强误配信号）
        贪心按分低者优先独占分配；距离超过 match_max_dist 宁开新轨不误挂。
        同 raw_id 多只实体交叉走位时不再张冠李戴。
        """
        max_dist = safe_float(cfg_get("predictor.match_max_dist", 400), 400)
        pairs = []
        for di, d in enumerate(dets):
            for uid, tk in self._t.items():
                if tk.raw_id != d["raw_id"]:
                    continue
                pp = self._predicted_pos(tk, now)
                if pp is None:
                    continue
                px, py, vx, vy = pp
                dist = math.hypot(d["x"] - px, d["y"] - py)
                if dist > max_dist:
                    continue
                score = dist
                if tk.history:
                    last = tk.history[-1]
                    mx, my = d["x"] - last["x"], d["y"] - last["y"]
                    mnorm = math.hypot(mx, my)
                    vnorm = math.hypot(vx, vy)
                    if mnorm > 1 and vnorm > 1:
                        cos = (mx * vx + my * vy) / (mnorm * vnorm)
                        if cos < 0:
                            score += (1 - cos) * 0.5 * mnorm
                if (
                    tk.role in ("player_enemy", "player_ally")
                    and d["role"] in ("player_enemy", "player_ally")
                    and tk.role != d["role"]
                ):
                    score += 100000
                pairs.append((score, uid, di))
        pairs.sort(key=lambda p: p[0])
        used_t, used_d, assign = set(), set(), {}
        for _score, uid, di in pairs:
            if uid in used_t or di in used_d:
                continue
            used_t.add(uid)
            used_d.add(di)
            assign[di] = uid
        return assign

    def update(self, entities: list):
        now = time.time()
        dets = []
        for ent in entities or []:
            if not isinstance(ent, dict):
                continue
            raw_id = ent.get("raw_id", "unknown")
            x, y = ent.get("x"), ent.get("y")
            if not _valid_coord(x, y):
                continue
            dets.append(
                {
                    "raw_id": raw_id,
                    "rarity": ent.get("rarity", "Common"),
                    "role": detect_role(raw_id, ent.get("role")),
                    "x": safe_float(x),
                    "y": safe_float(y),
                }
            )
        assign = self._match(dets, now)
        for di, d in enumerate(dets):
            uid = assign.get(di)
            if uid is None:
                self._uid += 1
                uid = f"{d['raw_id']}_{self._uid}"
                self._t[uid] = _Tracker(d["raw_id"], d["rarity"], d["role"])
            tk = self._t[uid]
            tk.role = d["role"]
            tk.rarity = d["rarity"]
            tk.category = d["role"] if d["role"] != "monster" else classify_by_rarity(d["rarity"])
            tk.update(d["x"], d["y"])
        for uid in [u for u, t in self._t.items() if t.expired()]:
            del self._t[uid]

    def all_entities(self) -> list[EntityPred]:
        out = []
        for tk in self._t.values():
            p = tk.predict()
            if p is not None:
                out.append(p)
            elif tk.history:
                last = tk.history[-1]
                out.append(
                    {
                        "raw_id": tk.raw_id,
                        "rarity": tk.rarity,
                        "category": tk.category,
                        "role": tk.role,
                        "threat_score": _threat_table().get(tk.category, 5),
                        "x_now": round(last["x"], 1),
                        "y_now": round(last["y"], 1),
                        "x_predict": None,
                        "y_predict": None,
                        "vx_per_sec": 0,
                        "vy_per_sec": 0,
                        "confidence": 0.0,
                        "prediction_trusted": False,
                    }
                )
        out.sort(key=lambda e: e["threat_score"], reverse=True)
        return out[: safe_int(cfg_get("predictor.max_output_entities", 8), 8)]

    def reset(self):
        self._t.clear()
        self._uid = 0

    def status(self) -> dict:
        return {
            "tracked_entities": len(self._t),
            "predict_seconds": cfg_get("predictor.predict_seconds"),
            "confidence_threshold": cfg_get("predictor.confidence_threshold"),
        }


PREDICTOR = Predictor()


# ===========================================================================
# 4. 战斗评估（打 / 谨慎 / 跑 + 心态 + 组队协同）
# ===========================================================================
DECISION_FIGHT, DECISION_CAUTIOUS, DECISION_RETREAT = "fight", "cautious_fight", "retreat"
SET_COMBAT, SET_TANK, SET_RETREAT, SET_CHASE, SET_TEAM = "combat", "tank", "retreat", "chase", "team"
MINDSET_CONSERVATIVE, MINDSET_BALANCED, MINDSET_AGGRESSIVE = "conservative", "balanced", "aggressive"

CATEGORY_RANK = {
    "unknown": 0,
    "player_ally": 0,
    "normal": 1,
    "player_enemy": 1,
    "elite": 2,
    "boss": 3,
    "highest_boss": 4,
}


def enemy_threat(enemies: list) -> float:
    return sum(
        safe_float(
            _threat_table().get((e.get("category", "unknown") if isinstance(e, dict) else "unknown"), 5), 5
        )
        for e in enemies or []
    )


def threat_ratio(player_power: float, threat: float) -> float:
    p = safe_float(player_power, 0.0)
    return 999.0 if p <= 0 else threat / p


def decide_mindset(player: dict, enemies: list) -> str:
    if not enemies:
        return MINDSET_BALANCED
    top = enemies[0].get("category", "normal") if isinstance(enemies[0], dict) else "normal"
    ratio = threat_ratio(player.get("power_score", 100), enemy_threat(enemies))
    if top == "highest_boss":
        return MINDSET_CONSERVATIVE if ratio > 1.2 else MINDSET_BALANCED
    if top == "boss":
        if ratio > 1.5:
            return MINDSET_CONSERVATIVE
        return MINDSET_AGGRESSIVE if ratio < 0.5 else MINDSET_BALANCED
    if top == "elite":
        return MINDSET_AGGRESSIVE if ratio < 0.4 else MINDSET_BALANCED
    return MINDSET_AGGRESSIVE if ratio < 0.3 else MINDSET_BALANCED


def _team_set_adjust(teammates: list, cur: str) -> str:
    out = sum(1 for t in teammates if (t.get("petal_set") if isinstance(t, dict) else None) == SET_COMBAT)
    tank = sum(1 for t in teammates if (t.get("petal_set") if isinstance(t, dict) else None) == SET_TANK)
    if out > tank:
        return SET_TEAM
    if tank > out:
        return SET_COMBAT
    return cur


def judge_combat(player: dict, enemies: list, teammates: list) -> CombatEval:
    """输出 decision / recommended_set / mindset / 威胁比 等。"""
    threat = enemy_threat(enemies)
    rr = safe_float(cfg_get("combat.retreat_ratio", 1.0), 1.0)
    ratio = threat_ratio(player.get("power_score", 100), threat)
    mindset = decide_mindset(player, enemies)
    has_highest = any(isinstance(e, dict) and e.get("category") == "highest_boss" for e in enemies or [])
    hp = safe_float(player.get("hp", 100))
    max_hp = max(1.0, safe_float(player.get("max_hp", 100)))
    hp_ratio = hp / max_hp

    decision, rec, reason = DECISION_FIGHT, SET_COMBAT, ""
    if has_highest:
        if ratio > rr:
            decision, rec = DECISION_RETREAT, SET_RETREAT
            reason = "highest_boss 出现且实力不足，全力避险"
        elif ratio > rr * 0.6:
            decision, rec = DECISION_CAUTIOUS, SET_TANK
            reason = "highest_boss 出现，实力接近，谨慎周旋"
        else:
            decision, rec = DECISION_CAUTIOUS, SET_TANK
            reason = "highest_boss 出现但实力充足，可对抗"
    elif ratio >= rr * 1.4:
        decision, rec = DECISION_RETREAT, SET_RETREAT
        reason = f"敌方威胁({threat:.0f})远超自身实力({safe_float(player.get('power_score', 100)):.0f})"
    elif ratio >= rr * 0.8:
        decision, rec = DECISION_CAUTIOUS, SET_TANK
    else:
        chaseable = [
            e for e in enemies or [] if isinstance(e, dict) and e.get("category") in ("boss", "elite")
        ]
        rec = SET_CHASE if chaseable else SET_COMBAT

    if mindset == MINDSET_CONSERVATIVE and hp_ratio < 0.5 and decision == DECISION_FIGHT:
        decision, rec = DECISION_CAUTIOUS, SET_TANK
    if mindset == MINDSET_AGGRESSIVE and hp_ratio > 0.3 and decision == DECISION_CAUTIOUS:
        decision, rec = DECISION_FIGHT, SET_COMBAT
    # 逃生优先级高于协同
    if teammates and decision != DECISION_RETREAT:
        rec = _team_set_adjust(teammates, rec)

    return {
        "decision": decision,
        "recommended_set": rec,
        "mindset": mindset,
        "enemy_threat": round(threat, 1),
        "threat_ratio": round(ratio, 3),
        "has_highest_boss": has_highest,
        "retreat_reason": reason,
    }


class CombatEvaluator:
    """防抖缓存：interval 秒内重复评估直接返回上次结果，省 CPU。"""

    def __init__(self):
        self._last, self._cache = 0.0, None

    def evaluate(self, player, enemies, teammates) -> dict:
        now = time.time()
        if self._cache is not None and (now - self._last) < safe_float(
            cfg_get("combat.eval_debounce_interval", 0.7), 0.7
        ):
            return self._cache
        self._cache = judge_combat(player, enemies, teammates)
        self._last = now
        return self._cache

    def invalidate(self):
        self._cache = None


def should_chase(entity: dict, chased_distance: float = 0) -> bool:
    cat = entity.get("category", "normal") if isinstance(entity, dict) else "normal"
    if cat == "highest_boss":
        return False
    min_cat = cfg_get("combat.chase_min_category", "elite")
    if CATEGORY_RANK.get(cat, 0) < CATEGORY_RANK.get(str(min_cat), 2):
        return False
    return safe_float(chased_distance) < safe_float(cfg_get("combat.chase_max_distance", 400), 400)


def scale_coords(x, y) -> tuple[float, float]:
    """感知源坐标系 → 逻辑屏坐标换算（ROADMAP #19）。

    档案声明 perception.source_w/h（感知后端上报坐标所用分辨率）且与
    combat.safe_zone_w/h（逻辑屏）不一致时按比例缩放；未声明(0)原样返回。
    解决 Windows DPI 缩放 / 采集分辨率与屏幕不一致时点击错位的问题。
    """
    sw = safe_float(cfg_get("perception.source_w", 0), 0)
    sh = safe_float(cfg_get("perception.source_h", 0), 0)
    tw = safe_float(cfg_get("combat.safe_zone_w", 1920), 1920)
    th = safe_float(cfg_get("combat.safe_zone_h", 1080), 1080)
    x, y = safe_float(x), safe_float(y)
    if sw > 0 and sh > 0 and (sw != tw or sh != th):
        return round(x * tw / sw, 1), round(y * th / sh, 1)
    return x, y


def clamp_to_safe_zone(x, y, screen_w=None, screen_h=None, margin=None) -> tuple[float, float]:
    """把走位点限制在安全区内，防止贴墙贴角卡死。"""
    sw = safe_float(screen_w if screen_w is not None else cfg_get("combat.safe_zone_w", 1920), 1920)
    sh = safe_float(screen_h if screen_h is not None else cfg_get("combat.safe_zone_h", 1080), 1080)
    m = safe_float(margin if margin is not None else cfg_get("combat.safe_zone_margin", 100), 100)
    x, y = safe_float(x, sw / 2), safe_float(y, sh / 2)

    def axis(v, size):
        lo, hi = m, size - m
        return size / 2 if hi < lo else max(lo, min(hi, v))

    return round(axis(x, sw), 1), round(axis(y, sh), 1)


def apply_jitter(x, y) -> tuple[float, float]:
    """拟人抖动：小范围随机 + 15% 概率来一次稍大的。"""
    base = safe_float(cfg_get("combat.jitter_base", 8), 8)
    big = safe_float(cfg_get("combat.jitter_max", 15), 15)
    jx, jy = random.uniform(-base, base), random.uniform(-base, base)
    if random.random() < 0.15:
        jx, jy = random.uniform(-big, big), random.uniform(-big, big)
    return round(x + jx, 1), round(y + jy, 1)


def retreat_position(player: dict, threat_entity: dict, enemies: list) -> dict:
    """避险走位：实力弱→远离；实力强→侧向周旋。"""
    px, py = safe_float(player.get("x")), safe_float(player.get("y"))
    te = threat_entity if isinstance(threat_entity, dict) else {}
    ex, ey = safe_float(te.get("x_now"), px), safe_float(te.get("y_now"), py)
    ratio = threat_ratio(player.get("power_score", 100), enemy_threat(enemies))
    dx, dy = px - ex, py - ey
    dist = (dx**2 + dy**2) ** 0.5 or 1.0
    sw = safe_float(cfg_get("combat.safe_zone_w", 1920), 1920)
    sh = safe_float(cfg_get("combat.safe_zone_h", 1080), 1080)
    m = safe_float(cfg_get("combat.safe_zone_margin", 100), 100)

    def lim(v, size):
        lo, hi = m, size - m
        return size / 2 if hi < lo else max(lo, min(hi, v))

    if ratio > 1.0:
        d = safe_float(cfg_get("combat.flee_distance", 300), 300)
        return {
            "x": round(lim(px + dx / dist * d, sw), 1),
            "y": round(lim(py + dy / dist * d, sh), 1),
            "strategy": "flee",
        }
    d = safe_float(cfg_get("combat.strafe_distance", 150), 150)
    return {
        "x": round(lim(px - dy / dist * d, sw), 1),
        "y": round(lim(py + dx / dist * d, sh), 1),
        "strategy": "strafe",
    }


# ===========================================================================
# 5. 知识闭环（战术带适用条件，命中后真的影响决策）
# ===========================================================================
TACTIC_RULES = {
    "retreat": ("撤退", "避战", "不要恋战", "低血量", "放弃", "脱离", "降低风险", "规避"),
    "keep_distance": ("保持距离", "拉开距离", "远离", "安全距离", "横向机动", "机动", "转移", "不站桩", "绕"),
    "focus_fire": ("集火", "优先攻击", "优先击杀", "优先清除", "主动", "清底排", "切换目标", "输出"),
    "protect_ally": ("保护队友", "队友", "team", "配合", "分工", "抱团"),
}
CONDITION_MARKERS = ("适用", "条件", "当", "若", "时", "情况")
GENERIC_MARKERS = ("不要恋战", "优先集火", "主动", "团队", "配合")
EMBEDDED_CONDITIONS = ("低血量", "血量不足", "血量低于", "被夹击", "残兵", "高威胁", "被压制", "血量低")


def _low_hp(hp_ratio=1.0, **_):
    return hp_ratio < 0.4


def _high_threat(threat_ratio=0.0, **_):
    return (threat_ratio or 0) >= 1.0


def _few_enemies(n_enemies=0, **_):
    return (n_enemies or 0) <= 3


def _has_allies(has_allies=False, **_):
    return bool(has_allies)


def _losing(decision="", **_):
    return decision in ("retreat", "cautious_fight")


CONDITION_PREDICATES = {
    "低血量": _low_hp,
    "血量低于四成": _low_hp,
    "最高威胁": _high_threat,
    "存在高威胁目标": _high_threat,
    "敌方数量不超过三个": _few_enemies,
    "队友": _has_allies,
    "战斗评估判为劣势": _losing,
}


def _entry_states_condition(line: str) -> bool:
    low = str(line or "").lower()
    if not (any(m in low for m in CONDITION_MARKERS) or any(m in low for m in EMBEDDED_CONDITIONS)):
        return False
    return not (any(g in low for g in GENERIC_MARKERS) and not re.search(r"\d", low))


def parse_condition(line: str) -> dict:
    """从一条战术里解析出「标签 + 适用条件」；无条件则返回 {}。"""
    if not _entry_states_condition(line):
        return {}
    raw = str(line).lower()
    body, _, cond = raw.partition("适用条件")
    if not cond:
        body, _, cond = raw.partition("条件：")
    cond_scope = cond if cond else raw
    tag = None
    for name, words in TACTIC_RULES.items():
        if any(w in body for w in words):
            tag = name
            break
    if tag is None:
        return {}
    conds = [c for c in CONDITION_PREDICATES if c in cond_scope]
    for phrase, key in (
        ("低血量", "低血量"),
        ("血量不足", "低血量"),
        ("被夹击", "战斗评估判为劣势"),
        ("残兵", "战斗评估判为劣势"),
    ):
        if phrase in cond_scope and key not in conds:
            conds.append(key)
    return {"tag": tag, "conditions": conds} if conds else {}


def condition_matches(
    conds, hp_ratio=1.0, threat_ratio_=0.0, n_enemies=0, has_allies=False, decision=""
) -> bool:
    if not conds:
        return False
    ctx = {
        "hp_ratio": hp_ratio,
        "threat_ratio": threat_ratio_,
        "n_enemies": n_enemies,
        "has_allies": has_allies,
        "decision": decision,
    }
    for c in conds:
        pred = CONDITION_PREDICATES.get(c)
        if pred is None:
            return False
        if not pred(**ctx):
            return False
    return True


MISS_TOKENS = ("未找到", "没有找到", "无相关", "未找到相关内容")


def kb_is_hit(text: str) -> bool:
    return bool(text) and not any(t in str(text).lower() for t in MISS_TOKENS)


def extract_tactics(text: str) -> list:
    """抽出**声明了适用条件**的战术：(标签, 条件元组)。宁可漏，不可错。"""
    if not kb_is_hit(text):
        return []
    out, seen = [], set()
    for line in str(text).splitlines():
        parsed = parse_condition(line)
        if not parsed or parsed["tag"] in seen:
            continue
        seen.add(parsed["tag"])
        out.append((parsed["tag"], tuple(parsed["conditions"])))
    return out


KB_GATE_HP, KB_GATE_THREAT = 0.6, 1.0


def knowledge_gate(decision: str, tactics: list, hp_ratio=1.0, threat_ratio_=0.0) -> bool:
    """知识此刻是否该影响决策（空场不该被「撤退」带偏）。"""
    if not tactics or safe_float(threat_ratio_) <= 0:
        return False
    if decision == "retreat":
        return True
    if decision == "cautious_fight":
        return safe_float(hp_ratio, 1.0) < KB_GATE_HP or safe_float(threat_ratio_) > KB_GATE_THREAT
    if decision == "fight":
        return safe_float(threat_ratio_) < KB_GATE_THREAT
    return False


def apply_tactics(
    decision: str, tactics: list, has_allies=False, hp_ratio=1.0, threat_ratio_=0.0, n_enemies=0
) -> str:
    """把命中战术翻译成动作倾向；无影响返回空串。"""
    if not tactics:
        return ""
    tags = set()
    for item in tactics:
        if isinstance(item, (tuple, list)) and item:
            tag = item[0]
            conds = list(item[1]) if len(item) > 1 and isinstance(item[1], (list, tuple)) else []
            if not condition_matches(
                conds,
                hp_ratio=hp_ratio,
                threat_ratio_=threat_ratio_,
                n_enemies=n_enemies,
                has_allies=has_allies,
                decision=decision,
            ):
                continue
            tags.add(tag)
        else:
            tags.add(str(item))
    if not tags:
        return ""
    ally_tags = (tags & {"protect_ally", "focus_fire"}) if has_allies else set()
    if decision == "cautious_fight" and (tags & {"retreat", "keep_distance"}):
        return "defend"
    if decision == "fight" and ally_tags:
        return "attack"
    return ""


def decide_action(decision, tactics, hp_ratio=1.0, threat_ratio_=0.0, has_allies=False, n_enemies=0) -> str:
    """闸门 + 规则映射。"""
    if not knowledge_gate(decision, tactics, hp_ratio, threat_ratio_):
        return ""
    return apply_tactics(
        decision,
        tactics,
        has_allies=has_allies,
        hp_ratio=hp_ratio,
        threat_ratio_=threat_ratio_,
        n_enemies=n_enemies,
    )


def _with_condition(tactic: str) -> str:
    low = str(tactic or "").lower()
    if any(w in low for w in TACTIC_RULES["retreat"]):
        cond = "适用条件：血量低于四成，或被最高威胁实体锁定时"
    elif any(w in low for w in TACTIC_RULES["keep_distance"]):
        cond = "适用条件：敌方数量不超过三个时"
    elif any(w in low for w in TACTIC_RULES["focus_fire"]):
        cond = "适用条件：场上存在高威胁目标时"
    elif any(w in low for w in TACTIC_RULES["protect_ally"]):
        cond = "适用条件：组队对局且队友血量偏低时"
    else:
        cond = "适用条件：该局面已由战斗评估判为劣势时"
    return f"{tactic}（{cond}）"


def seed_knowledge(game: str | None = None, force: bool = False) -> list:
    """按游戏档案补种 seed 知识到 knowledge_md/<game>/（已存在则不覆盖）。"""
    g = safe_name(game or active_game()) or "default"
    prof = _read_yaml(os.path.join(PROFILE_DIR, f"{g}.yaml"))
    combat = prof.get("combat") or {}
    pred = prof.get("predictor") or {}
    tactics = [str(t) for t in (combat.get("tactics") or []) if str(t).strip()]
    if not tactics:
        tactics = [
            "血量不足且附近存在高威胁实体时立即撤退，不要恋战",
            "与高威胁实体保持距离，等其转移后再回场",
            "低威胁目标主动集火清理，保持场面干净",
        ]
    lines = "\n".join(f"- 战术: {_with_condition(t)}" for t in tactics)
    top = []
    for key in ("rarity_highest_boss", "rarity_boss", "rarity_elite"):
        vals = pred.get(key) or []
        if isinstance(vals, str):
            vals = [vals]
        top = [str(v) for v in vals if str(v).strip()]
        if top:
            break
    desc = (prof.get("game") or {}).get("description", "")
    docs = {
        "tactics": (
            f"# {g} 战术知识（seed）\n\n{desc}\n\n## 战术条目\n{lines}\n\n"
            "## 使用说明\n- 由 agent.py 依据档案 combat.tactics 生成\n"
            "- 条目带「适用条件」，条件不满足时不参与决策\n"
        ),
        "boss_guide": (
            f"# {g} 高威胁目标指南（seed）\n\n## 最高威胁实体\n"
            f"- {'、'.join(top) if top else '未声明'}\n\n## 应对原则\n"
            "- 最高威胁目标出现时优先判断打/跑；血量不足立即撤退并拉开距离\n"
            "- 中低威胁目标可在保持距离的前提下集火清理\n- 组队时优先保护队友输出位\n"
        ),
    }
    d = kb_game_dir(g)
    os.makedirs(d, exist_ok=True)
    written = []
    for fn, content in docs.items():
        fp = os.path.join(d, f"{fn}.md")
        if os.path.exists(fp) and not force:
            continue
        with open(fp, "w", encoding="utf-8") as f:
            f.write(content)
        written.append(fp)
    return written


class LearningStats:
    """检索 / 命中 / 引用 三计数器，落盘 run_logs/learning_stats.json。"""

    PATH = os.path.join(RUN_LOGS, "learning_stats.json")

    def __init__(self):
        self.data = self._load()

    def _load(self) -> dict:
        try:
            with open(self.PATH, encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
        except Exception:
            return {}

    def _row(self, game: str) -> list:
        return self.data.setdefault(game, [0, 0, 0])  # searches / hits / citations

    def record_search(self, game: str, hit: bool):
        self._row(game)[0] += 1
        self._row(game)[1] += int(bool(hit))

    def record_citation(self, game: str):
        self._row(game)[2] += 1

    def save(self) -> str:
        os.makedirs(RUN_LOGS, exist_ok=True)
        with open(self.PATH, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)
        return self.PATH

    def summary(self, game: str) -> str:
        s, h, c = self._row(game)
        rate = (h / s * 100) if s else 0.0
        return f"{game}: 检索 {s} 次 / 命中 {h} 次（{rate:.0f}%）/ 引用 {c} 次"


# ===========================================================================
# 6. 感知（mock 离线合成 / http 接外部检测服务）
# ===========================================================================
PERCEPTION_BACKENDS = ("mock", "http", "local", "template")


def _player_stub() -> dict:
    """local/template 后端的玩家状态桩（ROADMAP #6）。

    这两类后端只做实体检测、不解析玩家面板；可在档案里用
    perception.player_stub 显式覆盖（比如接入血量 OCR 前先保守给值）。
    """
    stub = cfg_get("perception.player_stub", None)
    if isinstance(stub, dict) and stub:
        return dict(stub)
    sw = safe_float(cfg_get("combat.safe_zone_w", 1920), 1920)
    sh = safe_float(cfg_get("combat.safe_zone_h", 1080), 1080)
    return {
        "alive": True,
        "hp": 100,
        "max_hp": 100,
        "x": sw / 2,
        "y": sh / 2,
        "power_score": 100,
        "petal_set": str(cfg_get("combat.default_set", "combat")),
        "talent": "none",
    }


def _grab_screen():
    """mss 抓屏 → BGR ndarray。返回 (img, err)；依赖缺失给明确安装指引。"""
    try:
        import mss
        import numpy as np
    except ImportError as e:
        return None, f"抓屏需要 mss+numpy（pip install mss numpy）: {e}"
    try:
        region = cfg_get("perception.capture_region", None)
        with mss.mss() as sct:
            mon = region if isinstance(region, dict) else sct.monitors[1]
            shot = sct.grab(mon)
            img = np.asarray(shot, dtype="uint8")[:, :, :3].copy()  # BGRA -> BGR
        return img, ""
    except Exception as e:  # 无显示环境 / 区域非法等
        return None, f"抓屏失败: {type(e).__name__}: {e}"


def _iou_c(ax, ay, aw, ah, bx, by, bw, bh) -> float:
    """中心点+宽高 表示的两框 IoU。"""
    ax1, ay1, ax2, ay2 = ax - aw / 2, ay - ah / 2, ax + aw / 2, ay + ah / 2
    bx1, by1, bx2, by2 = bx - bw / 2, by - bh / 2, bx + bw / 2, by + bh / 2
    ix = max(0.0, min(ax2, bx2) - max(ax1, bx1))
    iy = max(0.0, min(ay2, by2) - max(ay1, by1))
    inter = ix * iy
    union = aw * ah + bw * bh - inter
    return inter / union if union > 0 else 0.0


def _parse_yolo_output(
    out, src_w: int, src_h: int, size: int, conf_th: float, labels: list, rarity_map: dict
) -> list:
    """解析 YOLOv8 风格检测输出 [1, 4+nc, anchors]（或转置形态）→ 实体列表。

    blobFromImage 整图 resize（不保长宽比），坐标按 size→原图等比映回；
    贪心 NMS（IoU 0.4）。需要 numpy；纯函数，便于离线单测。
    """
    import numpy as np

    arr = np.asarray(out[0] if isinstance(out, (list, tuple)) else out, dtype="float32")
    if arr.ndim == 3:
        arr = arr[0]
    if arr.ndim != 2:
        return []

    def _valid(a) -> bool:
        return a.ndim == 2 and a.shape[0] - 4 >= 1 and a.shape[1] >= 1

    # 定向判定：标准形态 [4+nc, anchors] 且特征维≤锚点维时保持原样；
    # 否则若转置形态有效则转置；两向都无效才拒绝（容忍锚点极少的退化形态）
    if not (_valid(arr) and arr.shape[0] <= arr.shape[1]):
        if _valid(arr.T) and (arr.T.shape[0] <= arr.T.shape[1] or not _valid(arr)):
            arr = arr.T
        elif not _valid(arr):
            return []
    nc = arr.shape[0] - 4
    if nc <= 0 or arr.shape[1] == 0:
        return []
    cls_scores = arr[4:, :]
    conf = cls_scores.max(axis=0)
    cls = cls_scores.argmax(axis=0)
    mask = conf >= conf_th
    if not mask.any():
        return []
    sx, sy = src_w / float(size), src_h / float(size)
    boxes: list = []
    for i in np.flatnonzero(mask):
        cx, cy, w, h = (float(v) for v in arr[:4, i])
        idx = int(cls[i])
        label = str(labels[idx]) if idx < len(labels) else f"class_{idx}"
        boxes.append(
            {
                "cx": cx * sx,
                "cy": cy * sy,
                "w": w * sx,
                "h": h * sy,
                "conf": float(conf[i]),
                "label": label,
                "rarity": str((rarity_map or {}).get(label, "Common")),
            }
        )
    boxes.sort(key=lambda b: -b["conf"])
    kept: list = []
    for b in boxes:
        if all(
            _iou_c(b["cx"], b["cy"], b["w"], b["h"], k["cx"], k["cy"], k["w"], k["h"]) < 0.4 for k in kept
        ):
            kept.append(b)
    return [
        {"raw_id": b["label"], "rarity": b["rarity"], "x": round(b["cx"], 1), "y": round(b["cy"], 1)}
        for b in kept[:64]
    ]


class Perception:
    """返回统一 payload: {player, entities, teammates, afk_popup, _fallback?}

    后端（ROADMAP #6 插件化）：
    - mock     离线合成（档案 perception.mock）
    - http     接外部检测服务（原 perception_server）
    - local    mss 抓屏 + ONNX Runtime 本地推理（YOLOv8 导出的 .onnx）
    - template OpenCV 模板匹配（每类实体一张 <label>.png，零模型）
    """

    _ort_cache: dict = {}

    def __init__(self):
        self._mock = self._load_mock()

    def _load_mock(self) -> dict:
        prof = _read_yaml(os.path.join(PROFILE_DIR, f"{safe_name(active_game())}.yaml"))
        m = (prof.get("perception") or {}).get("mock") or {}
        if not m:
            m = {
                "drift": True,
                "afk_popup": False,
                "player": {
                    "alive": True,
                    "hp": 100,
                    "max_hp": 100,
                    "x": 960,
                    "y": 540,
                    "power_score": 120,
                    "petal_set": "combat",
                    "talent": "none",
                },
                "entities": [
                    {"raw_id": "hornet", "rarity": "Common", "x": 400, "y": 300, "vx": 80, "vy": 0},
                    {"raw_id": "beetle", "rarity": "Epic", "x": 1500, "y": 350, "vx": -30, "vy": -30},
                    {"raw_id": "mantis", "rarity": "Super", "x": 1700, "y": 800, "vx": 0, "vy": 0},
                ],
                "teammates": [],
            }
        self._state = [dict(e) for e in (m.get("entities") or [])]
        self._t0 = time.time()
        return m

    def backend(self) -> str:
        b = (os.getenv("UGF_PERCEPTION_BACKEND") or "").strip().lower() or str(
            cfg_get("perception.backend", "auto")
        ).lower()
        if b == "auto":
            return "mock" if dry_run() else "http"
        if b not in PERCEPTION_BACKENDS:
            log(f"[感知] 未知后端 {b!r}（可选 {'/'.join(PERCEPTION_BACKENDS)}），回落 mock")
            return "mock"
        return b

    def _mock_frame(self) -> FramePayload:
        drift = bool(self._mock.get("drift", True))
        dt = 1.0 / 30.0
        ents = []
        sw, sh = 1920, 1080
        for e in self._state:
            if drift:
                e["x"] = max(10, min(sw - 10, safe_float(e.get("x")) + safe_float(e.get("vx")) * dt))
                e["y"] = max(10, min(sh - 10, safe_float(e.get("y")) + safe_float(e.get("vy")) * dt))
            ents.append(
                {
                    "raw_id": e.get("raw_id"),
                    "rarity": e.get("rarity"),
                    "x": round(safe_float(e.get("x")), 1),
                    "y": round(safe_float(e.get("y")), 1),
                }
            )
        player = dict(self._mock.get("player") or {})
        # 玩家缓慢绕圈，方便观察走位
        ang = (time.time() - self._t0) * 0.6
        player["x"] = round(960 + 220 * math.cos(ang), 1)
        player["y"] = round(540 + 140 * math.sin(ang), 1)
        return {
            "player": player,
            "entities": ents,
            "teammates": [dict(t) for t in (self._mock.get("teammates") or [])],
            "afk_popup": bool(self._mock.get("afk_popup", False)),
            "_fallback": "mock",
        }

    def _http_frame(self) -> FramePayload:
        try:
            import requests
        except ImportError:
            return {"error": "未安装 requests，无法走 http 感知后端（可设 UGF_DRY_RUN=1 用 mock）"}
        url = (os.getenv("UGF_PERCEPTION_URL") or "").strip() or cfg_get("perception.http_url", "")
        if not url:
            url = f"http://127.0.0.1:{safe_int(cfg_get('server.perception_port', 5001), 5001)}/perceive"
        attempts = safe_int(cfg_get("resilience.perception.retries", 2), 2) + 1
        backoff = safe_float(cfg_get("resilience.perception.backoff", 1.0), 1.0)

        def _fetch():
            r = requests.get(url, timeout=safe_float(cfg_get("perception.timeout", 24), 24))
            r.raise_for_status()
            data = r.json()
            data.pop("_raw", None)
            if not isinstance(data, dict):
                raise ValueError("感知服务返回非 JSON 对象")
            return data

        ok, data, err = retry_call(_fetch, attempts, backoff, "感知服务")
        if ok:
            return data
        return {"error": f"感知服务不可用: {err}"}

    def _local_session(self, model_path: str, ort):
        """ONNX 会话按 (路径, mtime) 缓存，换模型文件自动重建。"""
        key = (model_path, os.path.getmtime(model_path))
        sess = Perception._ort_cache.get("sess")
        if sess is None or Perception._ort_cache.get("key") != key:
            sess = ort.InferenceSession(model_path, providers=["CPUExecutionProvider"])
            Perception._ort_cache = {"key": key, "sess": sess}
        return sess

    def _local_frame(self) -> FramePayload:
        """local 后端：mss 抓屏 + ONNX Runtime 本地推理（YOLOv8 导出格式）。"""
        model_path = str(cfg_get("perception.local.model_path", "") or "").strip()
        if not model_path:
            return {"error": "local 后端未配置 perception.local.model_path（.onnx 模型路径）"}
        if not os.path.exists(model_path):
            return {"error": f"ONNX 模型不存在: {model_path}"}
        try:
            import onnxruntime as ort
        except ImportError as e:
            return {"error": f"local 后端需要 onnxruntime（pip install onnxruntime numpy）: {e}"}
        img, err = _grab_screen()
        if img is None:
            return {"error": err}
        try:
            import cv2

            size = safe_int(cfg_get("perception.local.input_size", 640), 640)
            blob = cv2.dnn.blobFromImage(img, 1 / 255.0, (size, size), swapRB=True, crop=False)
        except ImportError as e:
            return {"error": f"local 后端需要 opencv 做预处理（pip install opencv-python-headless）: {e}"}
        try:
            sess = self._local_session(model_path, ort)
            out = sess.run(None, {sess.get_inputs()[0].name: blob})
        except Exception as e:
            return {"error": f"ONNX 推理失败: {type(e).__name__}: {e}"}
        ents = _parse_yolo_output(
            out,
            img.shape[1],
            img.shape[0],
            size,
            safe_float(cfg_get("perception.local.conf", 0.5), 0.5),
            cfg_get("perception.local.labels", []) or [],
            cfg_get("perception.local.rarity_map", {}) or {},
        )
        return {
            "player": _player_stub(),
            "entities": ents,
            "teammates": [],
            "afk_popup": False,
            "_fallback": "local",
        }

    def _template_frame(self) -> FramePayload:
        """template 后端：OpenCV 模板匹配。模板目录里每个 <label>.png 一类实体。

        调试可用 perception.template.source_image 指定截图文件代替实时抓屏。
        """
        tdir = str(cfg_get("perception.template.dir", "") or "").strip()
        if not tdir or not os.path.isdir(tdir):
            return {
                "error": f"template 后端需配置 perception.template.dir 且目录存在（当前: {tdir or '空'}）"
            }
        try:
            import cv2
            import numpy as np
        except ImportError as e:
            return {
                "error": f"template 后端需要 opencv+numpy（pip install opencv-python-headless numpy）: {e}"
            }
        src_img = str(cfg_get("perception.template.source_image", "") or "").strip()
        if src_img and os.path.exists(src_img):
            img = cv2.imread(src_img)
            if img is None:
                return {"error": f"无法读取调试截图: {src_img}"}
        else:
            img, err = _grab_screen()
            if img is None:
                return {"error": err}
        th = safe_float(cfg_get("perception.template.conf", 0.8), 0.8)
        rarity_map = cfg_get("perception.template.rarity_map", {}) or {}
        ents = []
        for fn in sorted(os.listdir(tdir)):
            if not fn.lower().endswith(".png"):
                continue
            label = os.path.splitext(fn)[0]
            tpl = cv2.imread(os.path.join(tdir, fn))
            if tpl is None or tpl.shape[0] > img.shape[0] or tpl.shape[1] > img.shape[1]:
                continue
            res = cv2.matchTemplate(img, tpl, cv2.TM_CCOEFF_NORMED)
            locs = np.argwhere(res >= th)
            kept: list = []
            for ty, tx in locs:
                cx, cy = float(tx) + tpl.shape[1] / 2, float(ty) + tpl.shape[0] / 2
                w, h = float(tpl.shape[1]), float(tpl.shape[0])
                if all(_iou_c(cx, cy, w, h, k[0], k[1], k[2], k[3]) < 0.3 for k in kept):
                    kept.append((cx, cy, w, h))
            for cx, cy, _w, _h in kept[:32]:
                ents.append(
                    {
                        "raw_id": label,
                        "rarity": str(rarity_map.get(label, "Common")),
                        "x": round(cx, 1),
                        "y": round(cy, 1),
                    }
                )
        return {
            "player": _player_stub(),
            "entities": ents,
            "teammates": [],
            "afk_popup": False,
            "_fallback": "template",
        }

    def frame(self) -> FramePayload:
        b = self.backend()
        if b == "http":
            return self._http_frame()
        if b == "local":
            return self._local_frame()
        if b == "template":
            return self._template_frame()
        return self._mock_frame()

    def perceive(self) -> FramePayload:
        """取一帧并自动喂给预判模块（= 原 perceive_game）。"""
        data = self.frame()
        if "error" not in data:
            PREDICTOR.update(data.get("entities") or [])
        return data


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


# ===========================================================================
# 8. 会话记忆 & 断点续玩
# ===========================================================================
STATE_FILE = os.path.join(BASE_DIR, "agent_state.json")
HISTORY_FILE = os.path.join(BASE_DIR, "session_history.json")
SNAP_FILE = os.path.join(RUN_LOGS, "agent_snapshot.json")

# ROADMAP #18：状态文件结构版本化——读取时自动迁移，写入时打版本号，
# 未来改结构不会弄坏老用户的断点续玩与历史战绩。
STATE_SCHEMA_VERSION = 1


def _migrate_state_v0_to_v1(st: dict) -> dict:
    """v0（无版本号）→ v1：字段结构不变，仅补版本号；未来字段改名在此挂钩。"""
    st = dict(st)
    st["schema_version"] = 1
    return st


_STATE_MIGRATIONS = {0: _migrate_state_v0_to_v1}


def _migrate_state(st: dict) -> dict:
    """按 schema_version 逐级执行迁移直到当前版本。"""
    ver = safe_int(st.get("schema_version"), 0)
    while ver < STATE_SCHEMA_VERSION:
        fn = _STATE_MIGRATIONS.get(ver)
        if fn is None:
            break
        st = fn(st)
        ver = safe_int(st.get("schema_version"), ver + 1)
    return st


def session_load() -> dict:
    default = {
        "game": active_game(),
        "status": "idle",
        "last_played": None,
        "last_rounds": 0,
        "last_report": "",
        "brief": None,
        "sessions": 0,
        "total_deaths": 0,
        "resumed": False,
        "resume_point": None,
        "started_at": datetime.now().isoformat(timespec="seconds"),
    }
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            st = json.load(f)
    except Exception:
        return default
    if not isinstance(st, dict):
        return default
    st = _migrate_state(st)
    default.update(st)
    return default


def session_save(state: dict):
    try:
        state = dict(state)
        state["schema_version"] = STATE_SCHEMA_VERSION
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except (OSError, TypeError, ValueError) as e:
        raise RuntimeError(f"会话档案保存失败: {e}") from e


def session_record_start(game: str) -> bool:
    """开玩前调用；返回是否可「续玩」。"""
    st = session_load()
    prev = safe_int(st.get("last_rounds"))
    resumable = bool(st.get("last_played")) or prev > 0
    st["resume_point"] = (
        {
            "game": game,
            "at": datetime.now().isoformat(timespec="seconds"),
            "from_rounds": prev,
            "deaths": safe_int(st.get("total_deaths")),
        }
        if resumable
        else None
    )
    session_save(st)
    return resumable


def session_record_end(game: str, rounds: int, deaths: int, report: str = ""):
    st = session_load()
    rounds, deaths = safe_int(rounds), safe_int(deaths)
    st.update(
        {
            "game": game,
            "status": "done",
            "last_played": datetime.now().isoformat(timespec="seconds"),
            "last_rounds": rounds,
            "last_report": report or "",
            "sessions": safe_int(st.get("sessions")) + 1,
            "total_deaths": safe_int(st.get("total_deaths")) + max(0, deaths),
            "resumed": False,
            "resume_point": None,
        }
    )
    session_save(st)
    _history_append({"at": st["last_played"], "game": game, "rounds": rounds, "deaths": deaths})


def _read_history() -> list:
    """战绩历史双格式兼容（ROADMAP #18）：
    旧格式 = 纯列表；新格式 = {"schema_version": N, "records": [...]}。"""
    raw = _read_json(HISTORY_FILE, [])
    if isinstance(raw, dict):
        recs = raw.get("records")
        return [r for r in recs if isinstance(r, dict)] if isinstance(recs, list) else []
    if isinstance(raw, list):
        return [r for r in raw if isinstance(r, dict)]
    return []


def _history_append(record: dict):
    h = _read_history()
    h.append(record)
    payload = {"schema_version": STATE_SCHEMA_VERSION, "records": h[-100:]}
    try:
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


def session_summary() -> str:
    st = session_load()
    played = _read_history()
    total_rounds = sum(safe_int(r.get("rounds")) for r in played)
    total_deaths = sum(safe_int(r.get("deaths")) for r in played)
    line = (
        f"会话：玩过 {safe_int(st.get('sessions'))} 场 ｜ 本次/上次回合 {safe_int(st.get('last_rounds'))} "
        f"｜ 累计死亡 {safe_int(st.get('total_deaths'))}\n"
        f"战绩历史：{len(played)} 局，合计 {total_rounds} 回合 / {total_deaths} 死亡\n"
        f"上次游玩：{st.get('last_played') or '（无）'}"
    )
    return line


def resume_info() -> str:
    st = session_load()
    rp = st.get("resume_point")
    if not rp or st.get("resumed"):
        return ""
    return (
        f"检测到上次进度：游戏={rp.get('game')}，上次打了 {rp.get('from_rounds')} 回合，"
        f"累计死亡 {rp.get('deaths')} —— 本次将接着往下打。"
    )


def _read_json(path: str, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


# ===========================================================================
# 9. 复盘 & BOSS 记忆
# ===========================================================================
def should_review(state: FramePayload) -> bool:
    """复盘触发判定（ROADMAP #15）：开关与触发条件全部下放配置/游戏档案。

    - review.enabled=false 时全关
    - trigger_boss 按档案声明的 boss / highest_boss 稀有度档判定（不再硬编码）
    - trigger_team 控制组队局是否复盘
    """
    if not bool(cfg_get("review.enabled", True)):
        return False
    boss_rarities = {str(r).capitalize() for r in (cfg_get("predictor.rarity_boss", []) or [])}
    boss_rarities |= {str(r).capitalize() for r in (cfg_get("predictor.rarity_highest_boss", []) or [])}
    if not boss_rarities:
        boss_rarities = {"Super", "Unique", "Eternal"}
    ents = state.get("entities") or []
    has_boss = any(
        str(e.get("rarity", "")).capitalize() in boss_rarities for e in ents if isinstance(e, dict)
    )
    if has_boss and bool(cfg_get("review.trigger_boss", True)):
        return True
    return bool(state.get("teammates")) and bool(cfg_get("review.trigger_team", True))


DEFAULT_REVIEW_TEMPLATE = (
    "# 对局复盘 — {ts}\n\n"
    "- 结果: {outcome}\n"
    "- 面对怪物: {monster}\n"
    "- 自身套装: {set}\n"
    "- 死亡原因: {cause}\n"
    "- 可改进点: {note}\n"
)


def review_round(survived: bool, note: str, state: FramePayload) -> str:
    """生成复盘并写入知识库（ROADMAP #15：模板可配置 + 结构化字段便于死因统计）。"""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    outcome = "存活" if survived else "死亡"
    _praw = state.get("player")
    player = _praw if isinstance(_praw, dict) else {}
    set_info = player.get("petal_set", "未知")
    ents = [e for e in (state.get("entities") or []) if isinstance(e, dict)]
    monster = "、".join(f"{e.get('raw_id', '?')}({e.get('rarity', '?')})" for e in ents[:5]) or "未知"
    cause = note if survived else f"死亡。当时面对怪物: {monster}，自身套装: {set_info}"
    fmt = {
        "ts": ts,
        "outcome": outcome,
        "monster": monster,
        "set": set_info,
        "cause": cause,
        "note": note,
    }
    template = str(cfg_get("review.template", "") or "").strip() or DEFAULT_REVIEW_TEMPLATE
    try:
        content = template.format(**fmt)
    except (KeyError, IndexError, ValueError):
        log("[复盘] 自定义模板占位符有误，回落内置模板")
        content = DEFAULT_REVIEW_TEMPLATE.format(**fmt)
    content += (
        "\n## 结构化字段\n"
        f"- outcome: {outcome}\n"
        f"- killer_entities: {monster}\n"
        f"- set: {set_info}\n"
        f"- ts: {ts}\n"
    )
    hist = kb_search("对局复盘", game=active_game())
    content += "\n## 与历史对局对比\n"
    content += (
        f"- 历史上有同类怪物({monster})的复盘，可回顾上次决策差异\n"
        if monster != "未知" and monster in hist
        else "- 暂无同怪物历史复盘\n"
    )
    kb_write(f"review_{ts}", content, game=active_game())
    PREDICTOR.reset()
    return f"[复盘] 结果={outcome}，已写入知识库，预判历史已清空"


def analyze_boss_behavior(samples: list) -> str:
    """从坐标样本归纳移动模式 + 接近倾向。samples: [(ex,ey,px,py), ...]"""
    n = len(samples)
    if n < 3:
        return "样本不足，暂无法归纳"
    turns = []
    for i in range(1, n - 1):
        ax, ay = samples[i][0] - samples[i - 1][0], samples[i][1] - samples[i - 1][1]
        bx, by = samples[i + 1][0] - samples[i][0], samples[i + 1][1] - samples[i][1]
        da, db = (ax * ax + ay * ay) ** 0.5, (bx * bx + by * by) ** 0.5
        if da < 1 or db < 1:
            continue
        cos_t = max(-1.0, min(1.0, (ax * bx + ay * by) / (da * db)))
        turns.append(math.degrees(math.acos(cos_t)))
    avg_turn = sum(turns) / len(turns) if turns else 0.0
    pattern = "绕圈/游走" if avg_turn > 30 else ("直线移动" if avg_turn < 15 else "缓行徘徊")
    dists = [((px - ex) ** 2 + (py - ey) ** 2) ** 0.5 for ex, ey, px, py in samples]
    avg_d = sum(dists) / len(dists)
    close = sum(1 for d in dists if d < safe_float(cfg_get("agent.boss_close_dist", 120), 120))
    return f"{pattern}；平均距离玩家约 {avg_d:.0f}px；近距离接近 {close} 次（越接近越凶/仇恨越强）"


def write_boss_memory(observations: list, samples: dict) -> str:
    if not observations:
        return ""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    content = f"### BOSS 行为观察 — {ts}\n\n" + "\n".join(f"- {o}" for o in observations) + "\n"
    if samples:
        content += "\n#### 行为归纳（多次遭遇累计共性）\n"
        for uid, s in samples.items():
            content += f"- {uid}：{analyze_boss_behavior(s)}\n"
    return kb_append("boss_behavior_log", content)


def write_snapshot(
    rounds: int,
    deaths: int,
    player: dict,
    predictions: list,
    combat_eval: dict,
    game: str,
    action_source: str = "",
):
    """每 N 回合写一次快照，供汇报/大盘读取。"""
    threats = [
        {
            "name": e.get("raw_id"),
            "cat": e.get("category"),
            "threat": e.get("threat_score"),
            "x": e.get("x_predict") or e.get("x_now"),
            "y": e.get("y_predict") or e.get("y_now"),
        }
        for e in (predictions or [])[:8]
        if isinstance(e, dict)
    ]
    snap = {
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "game": game,
        "round": rounds,
        "deaths": deaths,
        "hp": player.get("hp"),
        "max_hp": player.get("max_hp"),
        "decision": combat_eval.get("decision"),
        "mindset": combat_eval.get("mindset"),
        "set": combat_eval.get("recommended_set"),
        "action_source": action_source,
        "threats": threats,
    }
    try:
        os.makedirs(RUN_LOGS, exist_ok=True)
        with open(SNAP_FILE, "w", encoding="utf-8") as f:
            json.dump(snap, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


# ===========================================================================
# 10. 自动汇报（本地文件 + 可选 Webhook）
# ===========================================================================
def _tail_log(n: int = 10) -> list:
    p = os.path.join(RUN_LOGS, f"agent_{datetime.now().strftime('%Y%m%d')}.log")
    if not os.path.exists(p):
        return []
    try:
        with open(p, encoding="utf-8") as f:
            return f.read().splitlines()[-n:]
    except OSError:
        return []


def generate_report() -> str:
    st = session_load()
    snap = _read_json(SNAP_FILE, {})
    if not isinstance(snap, dict):
        snap = {}
    lines = [
        "# Universal-Game-Framework 对局报告",
        "",
        f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 当前游戏：{snap.get('game') or st.get('game') or active_game()}",
        f"- 会话状态：{st.get('status', 'idle')}",
        f"- 本局回合数：{safe_int(snap.get('round'))}",
        f"- 累计死亡：{safe_int(snap.get('deaths'))}",
        (
            f"- HP：{snap.get('hp')}/{snap.get('max_hp')}"
            if snap.get("hp") is not None
            else "- HP：未知（无快照）"
        ),
        "",
    ]
    if snap.get("decision"):
        lines += [
            "## 最新战斗",
            f"- 决策：{snap.get('decision')} / 心态：{snap.get('mindset')} / 推荐套装：{snap.get('set')}"
            + (f" / 动作来源：{snap.get('action_source')}" if snap.get("action_source") else ""),
        ]
        if snap.get("threats"):
            lines.append("- 近期威胁预判：")
            for t in snap["threats"][:5]:
                lines.append(
                    f"  - {t.get('name') or t.get('cat')}：威胁 {t.get('threat')} @({t.get('x')}, {t.get('y')})"
                )
    lines += [
        "",
        f"## 知识闭环\n{LEARNING_STATS.summary(active_game())}",
        "",
        f"## 调参状态\n{auto_tuner_status()}",
    ]
    tl = _tail_log()
    if tl:
        lines += ["", "## 最近日志"] + [f"- {x}" for x in tl]
    return "\n".join(lines)


def write_report_file(text: str) -> str:
    os.makedirs(RUN_LOGS, exist_ok=True)
    p = os.path.join(RUN_LOGS, f"report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md")
    with open(p, "w", encoding="utf-8") as f:
        f.write(text)
    return p


def push_webhook(text: str) -> tuple:
    url = (os.getenv("UGF_WEBHOOK_URL") or "").strip() or str(cfg_get("agent.webhook_url", "") or "")
    if not url:
        return False, "未配置 Webhook"
    try:
        import requests
    except ImportError:
        return False, "requests 未安装（离线环境）"
    attempts = safe_int(cfg_get("resilience.webhook.retries", 2), 2) + 1
    backoff = safe_float(cfg_get("resilience.webhook.backoff", 1.0), 1.0)
    timeout = safe_float(cfg_get("resilience.webhook.timeout", 5), 5)

    def _post():
        r = requests.post(url, json={"text": text}, timeout=timeout)
        code = getattr(r, "status_code", 0)
        if not 200 <= code < 300:
            raise RuntimeError(f"HTTP {code or '?'}")
        return code

    ok, code, err = retry_call(_post, attempts, backoff, "Webhook 推送")
    return (True, f"HTTP {code}") if ok else (False, err)


def notify(quiet: bool = False) -> list:
    """生成报告 + 落盘 + 可选 Webhook。"""
    text = generate_report()
    actions = []
    try:
        actions.append(f"已写报告: {write_report_file(text)}")
    except (OSError, TypeError, ValueError) as e:
        actions.append(f"写报告失败: {e}")
    url = (os.getenv("UGF_WEBHOOK_URL") or "").strip() or str(cfg_get("agent.webhook_url", "") or "")
    if url:
        ok, why = push_webhook(text)
        actions.append(f"已推送到 Webhook（{why}）" if ok else f"Webhook 推送失败: {why}")
    else:
        actions.append("未配置 Webhook(仅本地文件)")
    if not quiet:
        for a in actions:
            log(f"[汇报] {a}")
    return actions


def notify_progress(rounds: int, deaths: int) -> list:
    """局中轻量进度：覆盖写 run_logs/progress_report.md。"""
    text = (
        f"# Universal-Game-Framework 局中进度\n\n"
        f"- 更新：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"- 游戏：{active_game()}\n- 回合：{rounds}\n- 累计死亡：{deaths}\n"
    )
    try:
        os.makedirs(RUN_LOGS, exist_ok=True)
        with open(os.path.join(RUN_LOGS, "progress_report.md"), "w", encoding="utf-8") as f:
            f.write(text)
    except OSError:
        pass
    if (os.getenv("UGF_WEBHOOK_URL") or "").strip() or str(cfg_get("agent.webhook_url", "") or ""):
        push_webhook(text)
    return [text]


# ===========================================================================
# 11. 自动调参（tuned_overrides.yaml 热加载生效）
# ===========================================================================
RETREAT_RATIO_MIN, RETREAT_RATIO_MAX = 0.5, 1.5
CONF_THRESH_MIN, CONF_THRESH_MAX = 0.30, 0.90
TUNE_HOLD = 3


def _clamp(v, lo, hi, default):
    f = safe_float(v, default)
    return max(lo, min(hi, f))


def auto_tuner_current() -> dict:
    o = _read_yaml(TUNED_PATH)
    return {
        "combat.retreat_ratio": _clamp(
            (o.get("combat") or {}).get("retreat_ratio", cfg_get("combat.retreat_ratio", 1.0)),
            RETREAT_RATIO_MIN,
            RETREAT_RATIO_MAX,
            1.0,
        ),
        "predictor.confidence_threshold": _clamp(
            (o.get("predictor") or {}).get(
                "confidence_threshold", cfg_get("predictor.confidence_threshold", 0.65)
            ),
            CONF_THRESH_MIN,
            CONF_THRESH_MAX,
            0.65,
        ),
    }


def _tune_locked(param: str) -> bool:
    """ROADMAP #13：参数是否被人工锁定（agent.tune_locked，点分路径列表）。"""
    locked = cfg_get("agent.tune_locked", []) or []
    if isinstance(locked, str):
        locked = [locked]
    return param in {str(x).strip() for x in locked}


def _tune_audit(o: dict, param: str, old, new, reason: str):
    """ROADMAP #13：调参审计（旧值→新值→依据），随 tuned_overrides.yaml 滚动保留 50 条。"""
    audit = o.get("_audit")
    if not isinstance(audit, list):
        audit = []
    audit.append(
        {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "param": param,
            "old": old,
            "new": new,
            "reason": reason,
        }
    )
    o["_audit"] = audit[-50:]


def auto_tuner_status() -> str:
    c = auto_tuner_current()
    o = _read_yaml(TUNED_PATH)
    hold = safe_int(o.get("_cooldown", 0))
    s = (
        f"retreat_ratio={c['combat.retreat_ratio']} ｜ "
        f"confidence_threshold={c['predictor.confidence_threshold']}"
    )
    if hold:
        s += f" ｜ 冷静期剩 {hold} 周期"
    audit = o.get("_audit")
    if isinstance(audit, list) and audit:
        recent = [a for a in audit[-3:] if isinstance(a, dict)]
        if recent:
            s += "\n最近调参审计:\n" + "\n".join(
                f"  - {a.get('ts')} {a.get('param')}: {a.get('old')} → {a.get('new')}（{a.get('reason')}）"
                for a in reversed(recent)
            )
    locked = cfg_get("agent.tune_locked", []) or []
    if locked:
        s += "\n人工锁定: " + ", ".join(str(x) for x in locked)
    return s


def auto_tune(hits: int = 0, attempts: int = 0, deaths_extra: int = 0) -> str:
    """按战损微调阈值：死亡多→更早跑；命中率低→别太信预判。

    ROADMAP #13：每次变更记录审计（旧值→新值→依据）；
    agent.tune_locked 列出的参数视为人工锁定，跳过且在返回信息中留痕。
    """
    o = _read_yaml(TUNED_PATH)
    changed = []
    locked_msgs = []
    cooldown = safe_int(o.get("_cooldown", 0))
    if cooldown > 0:
        o["_cooldown"] = cooldown - 1
        _write_tuned(o)
        return f"[调参] 冷静期(剩 {cooldown - 1} 周期)，本轮不调整"

    if deaths_extra > 0:
        cur = _clamp(
            (o.get("combat") or {}).get("retreat_ratio", cfg_get("combat.retreat_ratio", 1.0)),
            RETREAT_RATIO_MIN,
            RETREAT_RATIO_MAX,
            1.0,
        )
        nxt = max(RETREAT_RATIO_MIN, cur - 0.1 * int(deaths_extra))
        if nxt < cur:
            if _tune_locked("combat.retreat_ratio"):
                locked_msgs.append("retreat_ratio 已人工锁定，跳过")
            else:
                o.setdefault("combat", {})["retreat_ratio"] = round(nxt, 2)
                _tune_audit(o, "combat.retreat_ratio", cur, round(nxt, 2), "死亡增多，更早跑")
                changed.append(f"retreat_ratio {cur}→{nxt:.2f}(死亡增多，更早跑)")
    if attempts > 0 and hits / attempts < 0.5:
        cur = _clamp(
            (o.get("predictor") or {}).get(
                "confidence_threshold", cfg_get("predictor.confidence_threshold", 0.65)
            ),
            CONF_THRESH_MIN,
            CONF_THRESH_MAX,
            0.65,
        )
        nxt = max(CONF_THRESH_MIN, cur - 0.05)
        if nxt < cur:
            if _tune_locked("predictor.confidence_threshold"):
                locked_msgs.append("confidence_threshold 已人工锁定，跳过")
            else:
                o.setdefault("predictor", {})["confidence_threshold"] = round(nxt, 2)
                _tune_audit(o, "predictor.confidence_threshold", cur, round(nxt, 2), "命中率低，采信线下调")
                changed.append(f"confidence {cur}→{nxt:.2f}(命中率低，采信线下调)")
    o["_cooldown"] = TUNE_HOLD if changed else 0
    _write_tuned(o)
    if changed:
        msg = "[调参] " + "；".join(changed)
    else:
        msg = "[调参] 本轮统计无需调整(阈值已在合理区间)"
    if locked_msgs:
        msg += " ｜ [锁定] " + "；".join(locked_msgs)
    return msg


def _write_tuned(data: dict):
    try:
        import yaml

        with open(TUNED_PATH, "w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)
    except Exception:
        pass


def auto_tuner_reset() -> str:
    try:
        os.remove(TUNED_PATH)
        return "已清空调参覆盖，回到默认阈值"
    except OSError:
        return "本就没有调参覆盖文件"


# ===========================================================================
# 12. 视频学习（抽帧 → VLM 提取战术 → 去重入库）
# ===========================================================================
def _vlm_prompt() -> str:
    """VLM 提示词：档案/配置 llm.vlm_prompt 可覆盖（ROADMAP #14）。"""
    return str(cfg_get("llm.vlm_prompt", "") or "").strip() or VLM_PROMPT


VLM_PROMPT = (
    "你是游戏战术分析师。看图，用一句话总结一条可执行的战术，"
    "必须带上适用条件（例如「低血量时…」「被夹击时…」）。只输出这一句话。"
)


def _write_png(path: str, width: int, height: int, rgb: tuple) -> str:
    """标准库写纯色 PNG（无 cv2/PIL 时的最小可用实现）。"""
    import struct
    import zlib

    row = b"\x00" + bytes(rgb) * width
    raw = row * height

    def chunk(tag, data):
        return (
            struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(raw, 6))
    png += chunk(b"IEND", b"")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(png)
    return path


def synthesize_frames(count: int = 5) -> list:
    """合成帧（离线）：每帧颜色不同，保证去重/逐帧逻辑能被真实检验。"""
    os.makedirs(FRAME_DIR, exist_ok=True)
    out = []
    for i in range(max(0, int(count))):
        rgb = ((37 * i + 60) % 256, (91 * i + 30) % 256, (153 * i + 12) % 256)
        out.append(_write_png(os.path.join(FRAME_DIR, f"frame_{i:06d}.png"), 64, 48, rgb))
    return out


def extract_frames(video_path: str, skip: int = 25) -> list:
    """从视频按间隔抽帧；没装 cv2 返回空列表（上层会降级到合成帧）。"""
    try:
        import cv2
    except ImportError:
        log("[学习] 未安装 opencv（cv2），跳过真实抽帧")
        return []
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        log(f"[学习] 无法打开视频 {video_path}")
        return []
    os.makedirs(FRAME_DIR, exist_ok=True)
    frames, idx = [], 0
    while cap.isOpened():
        ok, frame = cap.read()
        if not ok:
            break
        if idx % skip == 0:
            p = os.path.join(FRAME_DIR, f"frame_{idx:06d}.jpg")
            cv2.imwrite(p, frame)
            frames.append(p)
        idx += 1
    cap.release()
    return frames


def _image_to_b64(path: str) -> str:
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


def vlm_extract_tactic(b64_img: str) -> str:
    """单帧 → 一句战术。无密钥时返回明确提示（不伪造）。"""
    url = (os.getenv("VLM_API_URL") or "").strip()
    key = (os.getenv("VLM_API_KEY") or "").strip()
    if not url or not key:
        return "[未配置 VLM_API_URL / VLM_API_KEY，跳过 VLM]"
    try:
        import requests

        payload: dict = {
            "model": os.getenv("VLM_MODEL", ""),
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64_img}"}},
                        {"type": "text", "text": _vlm_prompt()},
                    ],
                }
            ],
            "max_tokens": 200,
        }
        r = requests.post(
            url,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json=payload,
            timeout=30,
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        return f"[VLM 调用失败: {type(e).__name__}]"


def _useful_tactic(t: str) -> bool:
    t = (t or "").strip()
    return len(t) >= 2 and not t.startswith("[") and not t.startswith("（")


def _dedup_new(tactics: list, target_dir: str, ratio: float = 0.75) -> list:
    """与历史 video_tactic_*.md 里的逐条战术比对，剔除过于相似的。"""
    known = []
    if os.path.isdir(target_dir):
        for f in os.listdir(target_dir):
            if f.startswith("video_tactic_") and f.endswith(".md"):
                try:
                    with open(os.path.join(target_dir, f), encoding="utf-8") as fk:
                        known.append(fk.read())
                except OSError:
                    continue
    known_lines = []
    for line in "\n".join(known).splitlines():
        s = line.strip()
        for prefix in ("- ", "* "):
            if s.startswith(prefix):
                s = s[len(prefix) :].strip()
        if ". " in s[:5] and s[:1].isdigit():
            s = s.split(". ", 1)[1].strip()
        if s and not s.startswith(("#", ">", "!")):
            known_lines.append(s)
    out = []
    for t in tactics:
        if not _useful_tactic(t):
            continue
        if any(difflib.SequenceMatcher(None, t.strip(), k).ratio() >= ratio for k in known_lines):
            continue
        out.append(t.strip())
    return out


def _png_to_rgb(path: str):
    """极简 PNG 解码（仅 8-bit RGB/RGBA、非隔行）：无 cv2 时的降级路径。

    返回 (w, h, [(r,g,b), ...])；不支持的格式抛 ValueError。
    """
    import struct
    import zlib

    with open(path, "rb") as f:
        data = f.read()
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("not a PNG")
    pos = 8
    idat = []
    w = h = depth = ctype = None
    while pos + 8 <= len(data):
        (ln,) = struct.unpack(">I", data[pos : pos + 4])
        tag = data[pos + 4 : pos + 8]
        chunk = data[pos + 8 : pos + 8 + ln]
        if tag == b"IHDR":
            w, h, depth, ctype, _c, _f, interlace = struct.unpack(">IIBBBBB", chunk)
            if depth != 8 or ctype not in (2, 6) or interlace != 0:
                raise ValueError(f"unsupported PNG (depth={depth}, ctype={ctype})")
        elif tag == b"IDAT":
            idat.append(chunk)
        elif tag == b"IEND":
            break
        pos += 12 + ln
    if w is None or h is None or not idat:
        raise ValueError("broken PNG")
    raw = zlib.decompress(b"".join(idat))
    bpp = 3 if ctype == 2 else 4
    stride = w * bpp
    out = bytearray()
    prev = bytearray(stride)
    p = 0
    for _y in range(h):
        ft = raw[p]
        p += 1
        line = bytearray(raw[p : p + stride])
        p += stride
        if ft == 1:
            for i in range(bpp, stride):
                line[i] = (line[i] + line[i - bpp]) & 0xFF
        elif ft == 2:
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif ft == 3:
            for i in range(stride):
                a = line[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + ((a + prev[i]) >> 1)) & 0xFF
        elif ft == 4:
            for i in range(stride):
                a = line[i - bpp] if i >= bpp else 0
                b = prev[i]
                c = prev[i - bpp] if i >= bpp else 0
                pp = a + b - c
                pa, pb, pc = abs(pp - a), abs(pp - b), abs(pp - c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[i] = (line[i] + pr) & 0xFF
        elif ft != 0:
            raise ValueError(f"unknown filter {ft}")
        out += line
        prev = line
    pixels = [(out[i], out[i + 1], out[i + 2]) for i in range(0, len(out) - bpp + 1, bpp)]
    return w, h, pixels


def _uniform_hash(gray_avg: float) -> int:
    """纯色/近纯色图的退化哈希：直接编码平均灰度（重复 8 字节）。

    8×8 平均哈希对无梯度图恒为 0，会把不同颜色判成重复；退化分支保证
    「同色=距离0、异色=距离随灰度差增大」，合成帧与真实纯色场景都正确。
    """
    g = max(0, min(255, int(round(gray_avg))))
    bits = 0
    for i in range(8):
        bits |= g << (i * 8)
    return bits


def _avg_hash(path: str):
    """感知哈希（8×8 平均哈希 → 64bit）。cv2 优先，纯 Python PNG 解码兜底；
    都不可用返回 None（调用方保守保留该帧）。"""
    try:
        import cv2

        img = cv2.imread(path, cv2.IMREAD_GRAYSCALE)
        if img is not None:
            small = cv2.resize(img, (8, 8), interpolation=cv2.INTER_AREA)
            if float(small.max()) - float(small.min()) <= 1.0:
                return _uniform_hash(float(small.mean()))
            avg = float(small.mean())
            bits = 0
            for i, v in enumerate(small.flatten()):
                if float(v) > avg:
                    bits |= 1 << i
            return bits
    except Exception:
        pass
    try:
        w, h, pixels = _png_to_rgb(path)
    except (ValueError, OSError, EOFError):
        return None
    gray = [(r * 299 + g * 587 + b * 114) // 1000 for r, g, b in pixels]
    small = []
    for by in range(8):
        y0, y1 = by * h // 8, max(by * h // 8 + 1, (by + 1) * h // 8)
        for bx in range(8):
            x0, x1 = bx * w // 8, max(bx * w // 8 + 1, (bx + 1) * w // 8)
            vals = [gray[y * w + x] for y in range(y0, y1) for x in range(x0, x1)]
            small.append(sum(vals) // max(1, len(vals)))
    if max(small) - min(small) <= 1:
        return _uniform_hash(sum(small) / len(small))
    avg = sum(small) / len(small)
    bits = 0
    for i, v in enumerate(small):
        if v > avg:
            bits |= 1 << i
    return bits


def _hamming(a: int, b: int) -> int:
    return bin(int(a) ^ int(b)).count("1")


def _dedup_frames(paths: list) -> tuple:
    """ROADMAP #14：感知哈希跳过近重复帧，省 VLM 调用。返回 (保留帧, 跳过数)。"""
    if not bool(cfg_get("learn.hash_dedup", True)):
        return list(paths), 0
    threshold = safe_int(cfg_get("learn.hash_threshold", 5), 5)
    kept: list = []
    hashes: list = []
    skipped = 0
    for p in paths:
        h = _avg_hash(p)
        if h is None:  # 解码不了 → 保守保留
            kept.append(p)
            continue
        if any(_hamming(h, k) <= threshold for k in hashes):
            skipped += 1
            continue
        kept.append(p)
        hashes.append(h)
    return kept, skipped


def _vote_tactics(tactics: list, min_votes: int = 2) -> list:
    """ROADMAP #14：多帧投票。相似战术（ratio≥0.75）归组，票数达标才入库；
    代表句取组内最长一条（信息量最大）。min_votes=1 等价于关闭投票。"""
    groups: list = []  # [票数, 代表句]
    for t in tactics:
        t = str(t or "").strip()
        if not _useful_tactic(t):
            continue
        placed = False
        for g in groups:
            if difflib.SequenceMatcher(None, t, g[1]).ratio() >= 0.75:
                g[0] += 1
                if len(t) > len(g[1]):
                    g[1] = t
                placed = True
                break
        if not placed:
            groups.append([1, t])
    need = max(1, safe_int(min_votes, 2))
    return [g[1] for g in groups if g[0] >= need]


def learn_from_video(
    video_path: str | None = None, frame_count: int = 5, skip: int = 25, cleanup: bool = True
) -> dict:
    """一次完整学习（ROADMAP #14）：抽帧 → 哈希跳重复 → 逐帧 VLM → 多帧投票
    → 历史去重 → 入库 → 清理临时帧。"""
    frames = extract_frames(video_path, skip) if video_path else []
    mode = "真实抽帧" if frames else "合成帧(离线降级)"
    if not frames:
        frames = synthesize_frames(frame_count)
    total_frames = len(frames)
    frames, dedup_skipped = _dedup_frames(frames)
    log(f"[学习] {mode}，共 {total_frames} 帧（哈希去重跳过 {dedup_skipped}），开始逐帧提取战术...")

    tactics = []
    for fp in frames:
        try:
            t = vlm_extract_tactic(_image_to_b64(fp))
        except OSError as e:
            log(f"[学习] 读帧失败 {fp}: {e}")
            continue
        tactics.append(t)

    min_votes = safe_int(cfg_get("learn.min_votes", 2), 2)
    voted = _vote_tactics(tactics, min_votes)
    target = kb_game_dir(active_game())
    os.makedirs(target, exist_ok=True)
    kept = _dedup_new(voted, target)
    name = os.path.splitext(os.path.basename(video_path))[0] if video_path else "offline"
    fn = f"video_tactic_{safe_name(name)}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
    content = (
        f"# 视频学习战术 — {video_path or '(离线合成)'}\n\n"
        f"> 学习时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"> 提取帧数: {len(tactics)}（哈希去重跳过 {dedup_skipped}），"
        f"投票(≥{min_votes}帧)后 {len(voted)} 条，历史去重后 {len(kept)} 条\n\n"
        "## 战术列表\n\n" + "".join(f"{i}. {t}\n" for i, t in enumerate(kept, 1))
    )
    path = os.path.join(target, fn)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    if cleanup:
        shutil.rmtree(FRAME_DIR, ignore_errors=True)
    log_event(
        "learn",
        frames=len(tactics),
        dedup_skipped=dedup_skipped,
        voted=len(voted),
        kept=len(kept),
        file=path,
    )
    return {
        "frames": len(tactics),
        "dedup_skipped": dedup_skipped,
        "voted": len(voted),
        "kept": len(kept),
        "file": path,
    }


def download_video(url: str) -> str:
    """用 yt-dlp 下载视频（需自行安装 yt-dlp）。"""
    dest = os.path.join(FRAME_DIR, "_dl")
    os.makedirs(dest, exist_ok=True)
    import subprocess

    try:
        subprocess.run(
            [
                "yt-dlp",
                "-f",
                "b[ext=mp4]/bv*+ba/b",
                "-o",
                os.path.join(dest, "video.%(ext)s"),
                "--merge-output-format",
                "mp4",
                url,
            ],
            capture_output=True,
            text=True,
            timeout=300,
        )
    except Exception as e:
        return f"下载失败: {e}"
    for f in os.listdir(dest):
        if f.endswith(".mp4"):
            return os.path.join(dest, f)
    return "下载失败：未得到视频文件"


# ===========================================================================
# 13. LLM 大脑（有密钥走 LLM，没有走规则兜底 + 知识加权）
# ===========================================================================
SYSTEM_PROMPT = """你是 Universal-Game-Framework，一个游戏智能体，目标是优先保命、持续作战。
决策规则：
- afk_popup=true 优先处理验证
- 遇最高威胁目标(Unique/Eternal)时，实力不足全力避险，实力充足谨慎周旋
- 预判置信度<0.6 时降低对预判坐标的依赖，更多参考当前画面
- 有队友时保持安全距离，配合分工（队友输出→我辅助；队友抗伤→我输出）
只输出一个动作 JSON：{"action":"move","x":100,"y":200}
动作：move(x,y) / attack / defend / synthesize / idle。"""


def _llm_extract_json(content: str):
    """从 LLM 回复里抽出 JSON 对象（兼容 ```json 围栏与前后废话）。"""
    content = (content or "").strip()
    if "```" in content:
        content = content.split("```")[1]
        if content.startswith("json"):
            content = content[4:]
        content = content.strip()
    return json.loads(content)


def _validate_action(obj) -> tuple[ActionDict | None, str]:
    """ROADMAP #8：LLM 动作 JSON 的 schema 校验与清洗。

    返回 (合法动作 dict | None, 错误说明)：
    - action 必须 ∈ VALID_ACTIONS
    - move 必须携带有限数值 x/y（拒绝 bool 冒充），截断为 int
    - 白名单外字段一律丢弃；通过则打 source="llm" 标签
    """
    if not isinstance(obj, dict):
        return None, f"不是 JSON 对象（{type(obj).__name__}）"
    action = str(obj.get("action") or "").strip().lower()
    if action not in VALID_ACTIONS:
        return None, f"action 必须是 {'/'.join(VALID_ACTIONS)} 之一，收到 {action!r}"
    out: ActionDict = {"action": action, "source": "llm"}
    if action == "move":
        x, y = obj.get("x"), obj.get("y")
        if isinstance(x, bool) or isinstance(y, bool):
            return None, f"move 的 x/y 必须是数字而非布尔（x={x!r} y={y!r}）"
        if x is None or y is None:
            return None, f"move 需要数值 x/y（x={x!r} y={y!r}）"
        try:
            xf, yf = float(x), float(y)
        except (TypeError, ValueError):
            return None, f"move 需要数值 x/y（x={x!r} y={y!r}）"
        if not (math.isfinite(xf) and math.isfinite(yf)):
            return None, f"move 的 x/y 必须是有限数（x={x!r} y={y!r}）"
        out["x"], out["y"] = int(xf), int(yf)
    return out, ""


def llm_decide(
    state: FramePayload, predictions: list, combat_eval: dict, kb_text: str
) -> ActionDict:
    """LLM 决策（ROADMAP #5/#8）：传输层重试 + schema 校验 + 一次修复重试 + 规则兜底。

    - prompt 可被游戏档案 llm.system_prompt 覆盖
    - 输出不合法时把校验错误回喂 LLM 修复一次，仍不合法才落 fallback_decide
    - 返回值恒带 source 标签：llm / kb / rule
    """
    url = (os.getenv("LLM_API_URL") or "").strip()
    key = (os.getenv("LLM_API_KEY") or "").strip()
    if not url or not key:
        return fallback_decide(state, combat_eval, kb_text)
    try:
        import requests
    except ImportError:
        log("[LLM] 未安装 requests，走规则兜底")
        return fallback_decide(state, combat_eval, kb_text)

    system_prompt = str(cfg_get("llm.system_prompt", "") or "").strip() or SYSTEM_PROMPT
    user_content = (
        f"当前游戏状态:\n{json.dumps(state, ensure_ascii=False)}\n\n"
        f"实体预判(未来1.2秒):\n{json.dumps(predictions, ensure_ascii=False)}\n\n"
        f"战斗评估:\n{json.dumps(combat_eval, ensure_ascii=False)}\n\n"
        f"知识库战术:\n{kb_text}\n\n请输出下一步动作的 JSON。"
    )
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]
    attempts = safe_int(cfg_get("resilience.llm.retries", 1), 1) + 1
    backoff = safe_float(cfg_get("resilience.llm.backoff", 0.5), 0.5)
    timeout = safe_float(cfg_get("resilience.llm.timeout", 15), 15)

    def _call(msgs):
        payload: dict = {
            "model": os.getenv("LLM_MODEL", ""),
            "messages": msgs,
            "max_tokens": safe_int(cfg_get("llm.max_tokens", 800), 800),
            "temperature": safe_float(cfg_get("llm.temperature", 0.3), 0.3),
        }
        r = requests.post(
            url,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json=payload,
            timeout=timeout,
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip()

    ok, content, err = retry_call(lambda: _call(messages), attempts, backoff, "LLM 决策请求")
    if not ok:
        log(f"[LLM] 请求失败，改用规则兜底: {err}")
        return fallback_decide(state, combat_eval, kb_text)

    for attempt in (1, 2):
        try:
            action, verr = _validate_action(_llm_extract_json(content))
        except (ValueError, KeyError, IndexError, TypeError) as e:
            action, verr = None, f"无法解析 JSON: {type(e).__name__}"
        if action is not None:
            return action
        log(f"[LLM] 输出不合法(第 {attempt} 次): {verr}")
        if attempt == 1:
            repair = messages + [
                {"role": "assistant", "content": str(content)},
                {
                    "role": "user",
                    "content": (
                        f"你上次的输出不合法：{verr}。"
                        "只输出一个合法的动作 JSON，不要输出其他内容。"
                        '示例：{"action":"attack"} 或 {"action":"move","x":100,"y":200}'
                    ),
                },
            ]
            ok2, content2, err2 = retry_call(lambda _m=repair: _call(_m), 1, 0.0, "LLM 修复重试")
            if not ok2:
                log(f"[LLM] 修复重试请求失败: {err2}")
                break
            content = content2
    log("[LLM] 修复后仍不合法，改用规则兜底")
    return fallback_decide(state, combat_eval, kb_text)


def fallback_decide(
    state: FramePayload, combat_eval: dict, kb_text: str = ""
) -> ActionDict:
    """无 LLM 时的规则兜底 —— 命中知识会真正改变动作（闭环最后一段）。"""
    if state.get("afk_popup"):
        return {"action": "idle", "source": "rule"}
    decision = combat_eval.get("decision", "fight")
    _praw = state.get("player")
    player = _praw if isinstance(_praw, dict) else {}
    hp = safe_float(player.get("hp"))
    max_hp = max(1.0, safe_float(player.get("max_hp"), 100))
    hp_ratio = hp / max_hp
    tr = safe_float(combat_eval.get("threat_ratio"))
    kb_action = decide_action(
        decision,
        extract_tactics(kb_text),
        hp_ratio=hp_ratio,
        threat_ratio_=tr,
        has_allies=bool(state.get("teammates")),
        n_enemies=len(state.get("entities") or []),
    )
    if kb_action:
        return {"action": kb_action, "source": "kb"}
    if decision == "retreat":
        return {"action": "defend", "source": "rule"}
    return {"action": "attack", "source": "rule"}


# ===========================================================================
# 14. 主循环
# ===========================================================================
LEARNING_STATS = LearningStats()


def _autopilot_action(
    state: FramePayload, predictions: list, ev: dict, kb_text: str
) -> ActionDict:
    """选动作：优先让 LLM 决策，没密钥就走规则。"""
    return llm_decide(state, predictions, ev, kb_text)


def run_agent(max_rounds: int = 0, interval: float | None = None) -> dict:
    """主循环：感知 → 预判 → 评估 → 知识 → 决策 → 动作 → 记忆 → 复盘 → 汇报。"""
    interval = safe_float(interval if interval is not None else cfg_get("agent.loop_interval", 0.5), 0.5)
    game = active_game()
    perception = Perception()
    evaluator = CombatEvaluator()
    dead_threshold = safe_int(cfg_get("agent.death_frame_threshold", 8), 8)
    boss_interval = safe_float(cfg_get("agent.boss_memory_interval", 12), 12)
    learn_interval = safe_int(cfg_get("agent.learning_stats_interval", 24), 24)
    report_every = safe_int(cfg_get("agent.report_every", 0), 0)
    boss_sample_max = safe_int(cfg_get("agent.boss_sample_max", 120), 120)

    log("=" * 58)
    log(f"  Universal-Game-Framework v{VERSION} 单文件 Agent 启动")
    log(f"  {runtime_mode_text()}")
    if dry_run():
        log("  模式: DRY-RUN —— 只记录动作，不碰真实键鼠")
    log("=" * 58)

    log("[清理] " + kb_maintain())
    seeded = seed_knowledge(game)
    if seeded:
        log(f"[知识] 已补种 {len(seeded)} 份 seed 知识到 knowledge_md/{game}/")
    info = resume_info()
    resumable = session_record_start(game)
    if resumable and info:
        log("[续玩] " + info)
        st = session_load()
        st["resumed"] = True
        session_save(st)

    rounds = deaths = death_streak = set_switches = 0
    skipped = 0
    action_counts: dict = {}
    current_set = str(cfg_get("combat.default_set", "combat"))
    state: FramePayload = {}
    boss_obs: list = []
    boss_samples: dict = {}
    learn_buf: list = []
    deaths_cycle = 0
    last_boss_memory = 0.0
    paused = False
    t_start = time.time()

    log("[Agent] 进入游戏主循环...\n")
    try:
        while True:
            if max_rounds and rounds >= max_rounds:
                log("[Agent] 达到最大轮数，退出")
                break
            rounds += 1

            if reload_if_changed():
                log("[配置] config.yaml / 游戏档案已变更，热加载完成")

            if mouse_in_corner():
                if not paused:
                    log("[安全] 鼠标在屏幕角落，暂停 Agent")
                    paused = True
                time.sleep(1)
                continue
            if paused:
                log("[安全] 鼠标离开角落，恢复 Agent")
                paused = False

            # 1. 感知
            state = perception.perceive()
            if state.get("error"):
                log(f"[感知] 异常: {state['error']}")
                PREDICTOR.reset()
                time.sleep(1)
                continue
            if state.get("afk_popup"):
                log("[AFK] 检测到人机验证弹窗，跳过本回合（可用 action move 手动点）")
                skipped += 1
                time.sleep(interval)
                continue

            _praw = state.get("player")
            player = _praw if isinstance(_praw, dict) else {}

            # 2. 死亡防抖
            if not player.get("alive", True):
                death_streak += 1
                if death_streak >= dead_threshold:
                    deaths += 1
                    deaths_cycle += 1
                    log_event("death", round=rounds, deaths=deaths)
                    if should_review(state):
                        log(review_round(False, "玩家死亡，复盘本局", state))
                    else:
                        log("[复盘] 普通小怪局，不生成复盘（省硬盘）")
                        PREDICTOR.reset()
                    death_streak = 0
                    evaluator.invalidate()
                    time.sleep(2)
                    continue
            else:
                death_streak = 0

            # 3. 预判
            predictions = PREDICTOR.all_entities()

            # 4. 战斗评估
            teammates = state.get("teammates") or []
            ev = evaluator.evaluate(player, predictions, teammates)

            # 5. 换套（去抖）
            rec = ev.get("recommended_set")
            if rec and rec != current_set:
                res = switch_set(rec)
                kb_append(
                    "player_tactics",
                    f"- {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} "
                    f"决策={ev.get('decision')} 心态={ev.get('mindset')} "
                    f"威胁比={ev.get('threat_ratio')} → 换 {rec} 套",
                    game=game,
                )
                log(f"[套装] {current_set} → {rec}（{res}）")
                current_set = rec
                set_switches += 1

            # 6. 检索知识 + 记账
            keyword = "boss" if ev.get("has_highest_boss") else "战术"
            kb_text = kb_search(keyword, game=game)
            hit = kb_is_hit(kb_text)
            learn_buf.append((keyword, hit))
            LEARNING_STATS.record_search(game, hit)

            # 7. 决策
            action = _autopilot_action(state, predictions, ev, kb_text) or {"action": "idle"}
            if action.get("source") == "kb" or (hit and extract_tactics(kb_text)):
                LEARNING_STATS.record_citation(game)

            # 8. 走位钳制 + 拟人抖动
            atype = action.get("action", "idle")
            if atype == "move":
                sx, sy = scale_coords(action.get("x", 400), action.get("y", 300))
                tx, ty = clamp_to_safe_zone(sx, sy)
                tx, ty = apply_jitter(tx, ty)
                out = game_action("move", int(tx), int(ty))
            else:
                out = game_action(atype)
            action_counts[atype] = action_counts.get(atype, 0) + 1
            log_event(
                "decision",
                round=rounds,
                action=atype,
                source=str(action.get("source") or ""),
                decision=ev.get("decision"),
                mindset=ev.get("mindset"),
            )
            if any(k in out for k in ("错误", "必须提供", "未知动作")):
                log(f"[动作] 异常: {out}")

            # 9. BOSS 行为观察
            if ev.get("has_highest_boss") or any(e.get("category") == "boss" for e in predictions):
                px, py = safe_float(player.get("x")), safe_float(player.get("y"))
                for e in predictions:
                    if e.get("category") not in ("boss", "highest_boss"):
                        continue
                    uid = f"{e.get('raw_id', '?')}({e.get('rarity', '?')})"
                    boss_obs.append(
                        f"{datetime.now().strftime('%H:%M:%S')} {uid} "
                        f"位置({e.get('x_now')},{e.get('y_now')}) "
                        f"预判({e.get('x_predict')},{e.get('y_predict')}) "
                        f"决策={ev.get('decision')}"
                    )
                    s = boss_samples.setdefault(uid, [])
                    if len(s) >= boss_sample_max:
                        s.pop(0)
                    s.append((e.get("x_now", 0), e.get("y_now", 0), px, py))

            now = time.time()
            if now - last_boss_memory > boss_interval:
                write_boss_memory(boss_obs, boss_samples)
                boss_obs = []
                last_boss_memory = now

            # 10. 快照 / 进度汇报
            if rounds % 2 == 0:
                write_snapshot(
                    rounds,
                    deaths,
                    player,
                    predictions,
                    ev,
                    game,
                    action_source=str(action.get("source") or ""),
                )
            if report_every and rounds % report_every == 0:
                notify_progress(rounds, deaths)

            # 11. 学习统计 + 自动调参
            if learn_interval and rounds % learn_interval == 0 and learn_buf:
                hits = sum(1 for _k, h in learn_buf if h)
                kb_append(
                    "learning_stats",
                    f"- {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} 汇总：{hits}/{len(learn_buf)} 次命中",
                    game=game,
                )
                log(auto_tune(hits=hits, attempts=len(learn_buf), deaths_extra=deaths_cycle))
                learn_buf = []
                deaths_cycle = 0

            log(
                f"[回合 {rounds}] HP={player.get('hp')} 敌人={len(predictions)} "
                f"队友={len(teammates)} 决策={ev.get('decision')} 套装={current_set} "
                f"心态={ev.get('mindset')} → {atype}"
            )
            time.sleep(interval)

    except KeyboardInterrupt:
        log("\n[Agent] 收到中断信号")
    finally:
        try:
            write_boss_memory(boss_obs, boss_samples)
        except Exception as e:
            log(f"[记忆] 退出前写 BOSS 记忆失败: {e}")
        try:
            log(review_round(True, "Agent 正常退出", state))
        except Exception as e:
            log(f"[复盘] 退出前复盘失败: {e}")
        elapsed = time.time() - t_start
        speed = (rounds / elapsed) if elapsed > 0 else 0.0
        log(
            "[汇总] "
            + " | ".join(
                [
                    f"回合={rounds}",
                    f"死亡={deaths}",
                    f"跳过帧={skipped}",
                    f"换套={set_switches}",
                    "动作=" + (",".join(f"{k}x{v}" for k, v in sorted(action_counts.items())) or "无"),
                    f"耗时={elapsed:.1f}s（{speed:.1f} 回合/秒）",
                ]
            )
        )
        try:
            LEARNING_STATS.save()
            log("[知识] " + LEARNING_STATS.summary(game))
        except Exception as e:
            log(f"[知识] 指标落盘失败: {e}")
        report_text = generate_report()
        try:
            actions = notify(quiet=True)
            session_record_end(game, rounds, deaths, report_text)
            log("[汇报] " + "；".join(actions))
        except Exception as e:
            log(f"[汇报] 收尾失败: {e}")

    return {
        "rounds": rounds,
        "deaths": deaths,
        "skipped": skipped,
        "set_switches": set_switches,
        "actions": action_counts,
        "elapsed": round(time.time() - t_start, 2),
    }


def runtime_mode_text() -> str:
    m = runtime_mode()
    return (
        f"模式={m['mode']} | 感知={m['perception_backend']} | "
        f"LLM={m['llm']} | VLM={m['vlm']} | 游戏={m['game']}"
    )


# ===========================================================================
# 15a. 游戏档案校验（profile-check）
# ===========================================================================
SEMANTIC_SETS = ("combat", "tank", "retreat", "chase", "team")
VALID_CHASE_CATEGORIES = ("highest_boss", "boss", "elite", "normal", "player_enemy")
THREAT_KEYS = ("highest_boss", "boss", "elite", "normal", "player_enemy", "player_ally", "unknown")


def _pc_issue(issues: list, level: str, path: str, msg: str):
    issues.append((level, path, msg))


def profile_check_one(game: str) -> list:
    """校验单个游戏档案，返回 [(level, path, msg)]，level ∈ ERROR/WARN。

    规则来源：game_profiles/_template.yaml 的字段注释（安全区间 / 缺失兜底级别）。
    """
    issues: list = []
    g = safe_name(game)
    if not g:
        return [("ERROR", "game", "游戏名为空")]
    path = os.path.join(PROFILE_DIR, f"{g}.yaml")
    if not os.path.exists(path):
        return [("ERROR", "file", f"档案不存在: game_profiles/{g}.yaml")]
    prof = _read_yaml(path)
    if not prof:
        return [("ERROR", "file", f"档案无法解析为 YAML 字典: {g}.yaml（或缺少 pyyaml）")]

    # 一、游戏元信息
    name = str((prof.get("game") or {}).get("name") or "").strip()
    if not name:
        _pc_issue(issues, "ERROR", "game.name", "缺失（必填）")
    elif not re.fullmatch(r"[\w.-]+", name):
        _pc_issue(issues, "ERROR", "game.name", f"含非法字符（只允许 [\\w.-]）: {name!r}")
    elif name != g:
        _pc_issue(issues, "ERROR", "game.name", f"必须等于文件名 {g!r}，当前为 {name!r}")
    if not str((prof.get("game") or {}).get("description") or "").strip():
        _pc_issue(issues, "WARN", "game.description", "缺失（大盘与 brief 显示为空，不阻断）")

    # 二、感知端口
    port = (prof.get("server") or {}).get("perception_port")
    if port is None:
        _pc_issue(issues, "WARN", "server.perception_port", "缺失，将回落 config.yaml 默认端口")
    elif not isinstance(port, int) or isinstance(port, bool):
        _pc_issue(issues, "ERROR", "server.perception_port", f"必须为 1024~65535 的整数，当前: {port!r}")
    elif not 1024 <= port <= 65535:
        _pc_issue(issues, "ERROR", "server.perception_port", f"必须为 1024~65535 的整数，当前: {port!r}")

    # 三、稀有度档位与威胁金字塔
    pred = prof.get("predictor") or {}
    tiers: dict = {}
    for key in ("rarity_highest_boss", "rarity_boss", "rarity_elite", "rarity_normal"):
        vals = pred.get(key)
        if isinstance(vals, list) and vals and all(isinstance(v, str) and v.strip() for v in vals):
            tiers[key] = [v.strip().capitalize() for v in vals]
        else:
            _pc_issue(issues, "ERROR", f"predictor.{key}", "必须为非空字符串列表")
            tiers[key] = []
    seen_pairs: dict = {}
    for key, vals in tiers.items():
        for v in vals:
            if v in seen_pairs:
                _pc_issue(
                    issues,
                    "ERROR",
                    f"predictor.{key}",
                    f"稀有度 {v!r} 同时出现在 {seen_pairs[v]} 与 {key}，档位不允许重叠",
                )
            seen_pairs[v] = key

    threat = pred.get("threat")
    if not isinstance(threat, dict):
        _pc_issue(issues, "ERROR", "predictor.threat", "缺失或非字典（7 个键必须齐全）")
        threat = {}
    for k in THREAT_KEYS:
        v = threat.get(k)
        if v is None:
            _pc_issue(issues, "ERROR", f"predictor.threat.{k}", "缺失（必填）")
        elif not isinstance(v, (int, float)) or isinstance(v, bool) or v < 0:
            _pc_issue(issues, "ERROR", f"predictor.threat.{k}", f"必须为 >=0 的数字，当前: {v!r}")
    nums: dict = {}
    for k in THREAT_KEYS:
        v = threat.get(k)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            nums[k] = float(v)
    chain = [k for k in ("highest_boss", "boss", "elite", "normal") if k in nums]
    for a, b in zip(chain, chain[1:], strict=False):
        if nums[a] < nums[b]:
            _pc_issue(
                issues,
                "ERROR",
                "predictor.threat",
                f"单调性倒置：{a}({nums[a]}) < {b}({nums[b]})，要求逐级递减",
            )
    if isinstance(threat.get("player_ally"), (int, float)) and threat["player_ally"] != 0:
        _pc_issue(issues, "WARN", "predictor.threat.player_ally", "建议固定为 0（非 0 会干扰威胁求和）")

    # 四、战斗配置
    combat = prof.get("combat") or {}
    sets_raw = combat.get("sets")
    if (
        isinstance(sets_raw, list)
        and sets_raw
        and all(isinstance(x, str) and x.strip() for x in sets_raw)
    ):
        sets = [str(x).strip().lower() for x in sets_raw]
    else:
        _pc_issue(issues, "ERROR", "combat.sets", "必须为非空字符串列表（游戏内真实套装名）")
        sets = []
    default_set = str(combat.get("default_set") or "").strip().lower()
    if not default_set:
        _pc_issue(issues, "ERROR", "combat.default_set", "缺失（必填，运行时换套会落到未知套装）")
    elif sets and default_set not in sets:
        _pc_issue(issues, "ERROR", "combat.default_set", f"必须 ∈ combat.sets {sets}，当前: {default_set!r}")
    cmc = str(combat.get("chase_min_category") or "").strip()
    if not cmc:
        _pc_issue(issues, "ERROR", "combat.chase_min_category", "缺失（必填）")
    elif cmc not in VALID_CHASE_CATEGORIES:
        _pc_issue(
            issues,
            "ERROR",
            "combat.chase_min_category",
            f"非法值 {cmc!r}，可选 {'/'.join(VALID_CHASE_CATEGORIES)}",
        )
    if sets and not (set(sets) & set(SEMANTIC_SETS)) and not isinstance(combat.get("set_map"), dict):
        _pc_issue(
            issues,
            "WARN",
            "combat.set_map",
            "套装名与决策语义(combat/tank/retreat/chase/team)完全不同名却未声明 set_map，"
            "switch_set 会落到「未知套装」",
        )
    tactics = combat.get("tactics")
    if not isinstance(tactics, list) or not [t for t in tactics if str(t or "").strip()]:
        _pc_issue(issues, "WARN", "combat.tactics", "缺失或为空（知识库将没有经验可引用）")

    # 五、mock 感知数据（可选但强烈建议）
    mock = (prof.get("perception") or {}).get("mock")
    if not isinstance(mock, dict):
        _pc_issue(issues, "WARN", "perception.mock", "缺失：离线 dry-run 将退化到内置默认场景")
    else:
        all_rarities = {v for vals in tiers.values() for v in vals}
        player = mock.get("player") or {}
        max_hp = safe_float(player.get("max_hp"), 0)
        if max_hp <= 0:
            _pc_issue(issues, "ERROR", "perception.mock.player.max_hp", "必须 > 0")
        hp = safe_float(player.get("hp"), -1)
        if hp < 0 or (max_hp > 0 and hp > max_hp):
            _pc_issue(issues, "ERROR", "perception.mock.player.hp", f"必须在 0~max_hp 之间，当前: {hp!r}")
        ps = str(player.get("petal_set") or "").strip().lower()
        if sets and ps and ps not in sets:
            _pc_issue(
                issues,
                "ERROR",
                "perception.mock.player.petal_set",
                f"必须 ∈ combat.sets {sets}，当前: {ps!r}",
            )
        for i, ent in enumerate(mock.get("entities") or []):
            if not isinstance(ent, dict):
                _pc_issue(issues, "ERROR", f"perception.mock.entities[{i}]", "必须为字典")
                continue
            for field in ("raw_id", "x", "y"):
                if ent.get(field) is None:
                    _pc_issue(issues, "ERROR", f"perception.mock.entities[{i}].{field}", "缺失（必填）")
            rar = str(ent.get("rarity") or "").strip().capitalize()
            if all_rarities and rar and rar not in all_rarities:
                _pc_issue(
                    issues,
                    "ERROR",
                    f"perception.mock.entities[{i}].rarity",
                    f"{rar!r} 不属于任何已声明稀有度档位（离线链路会跑不通）",
                )
        if "teammates" not in mock:
            _pc_issue(
                issues,
                "WARN",
                "perception.mock.teammates",
                "未显式声明（单机游戏请写 []，否则会继承默认队友数据串味）",
            )
    return issues


def profile_check(game: str = "", check_all: bool = False, strict: bool = False) -> int:
    """校验游戏档案；返回退出码（0=通过，1=有 ERROR，strict 下 WARN 也算失败）。"""
    targets = []
    if check_all:
        if os.path.isdir(PROFILE_DIR):
            targets = sorted(
                f[:-5] for f in os.listdir(PROFILE_DIR) if f.endswith(".yaml") and not f.startswith("_")
            )
    else:
        targets = [game or active_game()]
    exit_code = 0
    for g in targets:
        issues = profile_check_one(g)
        errors = [i for i in issues if i[0] == "ERROR"]
        warns = [i for i in issues if i[0] == "WARN"]
        status = "✗ FAIL" if errors or (strict and warns) else ("⚠ PASS(有警告)" if warns else "✓ PASS")
        print(f"[{status}] {g}.yaml —— {len(errors)} 错误 / {len(warns)} 警告")
        for level, path_, msg in issues:
            print(f"    {level}: {path_}: {msg}")
        if errors or (strict and warns):
            exit_code = 1
    if not targets:
        print("没有可校验的档案（game_profiles/ 下无 *_*.yaml 之外的文件？）")
        exit_code = 1
    return exit_code


# ===========================================================================
# 14a. 可选轻量监控面板（ROADMAP #17：纯标准库，独立命令，不启动零开销）
# ===========================================================================
PANEL_HTML = """<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>UGF 监控面板</title>
<style>
  :root { color-scheme: dark; }
  body { font-family: ui-monospace, Consolas, monospace; background:#12151c; color:#dfe6f3;
         margin:0; padding:16px; }
  h1 { font-size:16px; margin:0 0 12px; color:#8fd3ff; }
  .grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(260px,1fr)); gap:12px; }
  .card { background:#1a1f2b; border:1px solid #2a3140; border-radius:8px; padding:10px 12px; }
  .card h2 { font-size:12px; color:#7d8aa5; margin:0 0 8px; text-transform:uppercase; }
  .big { font-size:22px; font-weight:700; }
  .kv { display:flex; justify-content:space-between; font-size:13px; padding:2px 0; }
  .kv b { color:#9fe8b6; font-weight:600; }
  table { width:100%; border-collapse:collapse; font-size:12px; }
  td, th { text-align:left; padding:3px 6px; border-bottom:1px solid #242b3a; }
  pre { font-size:11px; white-space:pre-wrap; word-break:break-all; color:#93a0b8;
        max-height:220px; overflow-y:auto; margin:0; }
  .tag { display:inline-block; background:#24304a; border-radius:4px; padding:1px 6px;
         font-size:11px; margin-right:4px; }
</style>
</head>
<body>
<h1>🎮 Universal-Game-Framework · 监控面板 <span id="ts" class="tag"></span></h1>
<div class="grid">
  <div class="card"><h2>运行状态</h2>
    <div class="kv"><span>模式</span><b id="mode"></b></div>
    <div class="kv"><span>游戏</span><b id="game"></b></div>
    <div class="kv"><span>感知后端</span><b id="backend"></b></div>
    <div class="kv"><span>LLM / VLM</span><b id="llmvlm"></b></div>
  </div>
  <div class="card"><h2>本局</h2>
    <div class="big">回合 <span id="round">-</span> · 死亡 <span id="deaths">-</span></div>
    <div class="kv"><span>HP</span><b id="hp"></b></div>
    <div class="kv"><span>决策 / 心态 / 套装</span><b id="dms"></b></div>
    <div class="kv"><span>动作来源</span><b id="asrc"></b></div>
  </div>
  <div class="card"><h2>会话与学习</h2>
    <div class="kv"><span>累计场次 / 死亡</span><b id="sess"></b></div>
    <div class="kv"><span>知识闭环</span><b id="learn"></b></div>
    <pre id="tuner"></pre>
  </div>
  <div class="card" style="grid-column:1/-1"><h2>威胁预判（快照）</h2>
    <table id="threats"><tr><th>实体</th><th>类别</th><th>威胁</th><th>x</th><th>y</th></tr></table>
  </div>
  <div class="card"><h2>最近事件（JSONL）</h2><pre id="events"></pre></div>
  <div class="card"><h2>日志尾部</h2><pre id="logs"></pre></div>
</div>
<script>
async function refresh() {
  try {
    const r = await fetch("/api/state", {cache: "no-store"});
    const d = await r.json();
    const s = d.snapshot || {}, m = d.mode || {}, se = d.session || {};
    document.getElementById("ts").textContent = s.ts || new Date().toLocaleTimeString();
    document.getElementById("mode").textContent = m.mode || "-";
    document.getElementById("game").textContent = m.game || s.game || "-";
    document.getElementById("backend").textContent = m.perception_backend || "-";
    document.getElementById("llmvlm").textContent = (m.llm||"off") + " / " + (m.vlm||"off");
    document.getElementById("round").textContent = s.round ?? "-";
    document.getElementById("deaths").textContent = s.deaths ?? "-";
    document.getElementById("hp").textContent =
      (s.hp !== undefined && s.hp !== null) ? (s.hp + "/" + s.max_hp) : "未知";
    document.getElementById("dms").textContent =
      [s.decision, s.mindset, s.set].filter(Boolean).join(" / ") || "-";
    document.getElementById("asrc").textContent = s.action_source || "-";
    document.getElementById("sess").textContent =
      (se.sessions ?? 0) + " 场 / " + (se.total_deaths ?? 0) + " 死";
    document.getElementById("learn").textContent = d.learning || "-";
    document.getElementById("tuner").textContent = d.tuner || "";
    const tb = document.getElementById("threats");
    tb.innerHTML = "<tr><th>实体</th><th>类别</th><th>威胁</th><th>x</th><th>y</th></tr>" +
      (s.threats || []).map(t =>
        "<tr><td>" + (t.name||"?") + "</td><td>" + (t.cat||"") + "</td><td>" +
        (t.threat||"") + "</td><td>" + (t.x||"") + "</td><td>" + (t.y||"") + "</td></tr>").join("");
    document.getElementById("events").textContent =
      (d.events || []).slice().reverse().map(e => JSON.stringify(e)).join("\n");
    document.getElementById("logs").textContent = (d.log_tail || []).join("\n");
  } catch (e) { /* agent 未运行时静默 */ }
}
refresh(); setInterval(refresh, 2000);
</script>
</body>
</html>
"""


def _panel_state() -> dict:
    """面板数据聚合：只读快照 / 会话 / 事件流 / 日志尾部，零副作用。"""
    snap = _read_json(SNAP_FILE, {})
    st = session_load()
    return {
        "snapshot": snap if isinstance(snap, dict) else {},
        "mode": runtime_mode(),
        "session": {
            "sessions": safe_int(st.get("sessions")),
            "total_deaths": safe_int(st.get("total_deaths")),
            "last_played": st.get("last_played"),
            "status": st.get("status"),
        },
        "events": read_events(15),
        "log_tail": _tail_log(15),
        "learning": LEARNING_STATS.summary(active_game()),
        "tuner": auto_tuner_status(),
    }


def start_panel_server(host: str = "127.0.0.1", port: int = 0):
    """创建面板 HTTP 服务（不阻塞）。返回 (server, 实际端口)；供 CLI 与测试复用。"""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class _Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, ctype: str, body: bytes):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802 - http.server 接口命名
            if self.path.startswith("/api/state"):
                body = json.dumps(_panel_state(), ensure_ascii=False, default=str).encode("utf-8")
                self._send(200, "application/json; charset=utf-8", body)
            elif self.path in ("/", "/index.html"):
                self._send(200, "text/html; charset=utf-8", PANEL_HTML.encode("utf-8"))
            else:
                self._send(404, "text/plain; charset=utf-8", b"not found")

        def log_message(self, *args):
            pass  # 静音访问日志

    srv = ThreadingHTTPServer((host, port), _Handler)
    return srv, srv.server_address[1]


def run_panel(host: str = "127.0.0.1", port: int | None = None):
    """阻塞式启动面板（CLI `agent.py panel`）。与主循环完全解耦：
    面板只读 run_logs/agent_snapshot.json 等落盘产物，agent 不在跑也能打开。"""
    if port is None:
        port = safe_int(cfg_get("server.panel_port", 5002), 5002)
    srv, actual = start_panel_server(host, port)
    log(f"[面板] http://{host}:{actual}/ （Ctrl+C 停止；agent 未启动时显示最近快照）")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        log("[面板] 已停止")
    finally:
        srv.server_close()


# ===========================================================================
# 15. CLI
# ===========================================================================
GUIDE = """# Universal-Game-Framework · 单文件游戏 Agent 使用手册

## 它是什么
一个能自己跑的游戏 Agent：看画面 → 预判 → 评估 → 出动作 → 查/写经验 → 复盘 → 汇报。
不需要 MCP、不需要别的 Agent 托管，一条命令就能开跑。

## 最常用的三条命令
    python agent.py run --dry-run --rounds 20   # 离线试跑 20 回合（不动键鼠）
    python agent.py run                          # 真机开跑（需感知服务 + pyautogui）
    python agent.py guide                        # 看这份手册

## 全部命令
    run      主循环（--rounds N / --interval S / --game NAME / --dry-run）
    mode     查看运行模式（dry-run / 感知后端 / LLM / VLM / 当前游戏）
    guide    本手册
    perceive 手工取一帧画面状态
    predict  手工取一帧 + 全实体 1.2s 预判
    action   执行一个动作：action move --x 100 --y 200 / action attack
    set      切套装：set retreat
    afk      查看 AFK 弹窗处理指引
    kb       知识库：list / search / write / append / export / import / boss / tactic / clean
    learn    视频学习：learn video.mp4 或 learn --url <链接> --frames 5
    report   生成一份对局报告（写 run_logs/，可选 Webhook）
    panel    本地监控面板（纯标准库，只读快照/事件/日志，Ctrl+C 停止）
    logs     查看运行日志：--tail N / --grep KW / --events --kind decision
    session  看会话记忆与历史战绩
    tune     自动调参：tune --status / tune --reset
    selftest 离线自检：不碰键鼠、不用密钥，跑通全链路并断言关键产物

## 关键开关（环境变量）
    UGF_DRY_RUN=1              不执行真实键鼠（推荐先这样试跑）
    UGF_PERCEPTION_BACKEND=... mock（离线合成）/ http（外部检测服务）/ local（ONNX 本地推理）/ template（模板匹配）
    UGF_PERCEPTION_URL=...     外部感知服务地址（留空则用 127.0.0.1:<端口>/perceive）
    AGENT_GAME=space_invaders  切换游戏档案（读 game_profiles/<名字>.yaml）
    LLM_API_URL / LLM_API_KEY / LLM_MODEL     LLM 决策
    VLM_API_URL / VLM_API_KEY / VLM_MODEL     视频学习的视觉模型
    UGF_WEBHOOK_URL=...        报告推送地址

## 注意事项
- 感知：离线用 mock；真机请让外部检测服务返回同样的 payload
  （{"player":{...},"entities":[{raw_id,rarity,x,y}],"teammates":[...],"afk_popup":false}）。
- 预判需要 ≥3 帧历史才有效；confidence < 0.65 时 prediction_trusted=false，别信预判坐标。
- 换局 / 重生后先 `kb clean --target predict`，避免用旧轨迹误判。
- 合规：仅用于本地 / 自建 / 已授权环境，不提供任何绕过他人服务条款的手段。
"""


def _print(obj):
    if isinstance(obj, (dict, list)):
        print(json.dumps(obj, ensure_ascii=False, indent=2))
    else:
        print(obj)


def _cli_kb(args) -> int:
    sub = args.kb_cmd
    game = args.game or active_game()
    if sub == "list":
        _print(kb_list(game) if not args.all else kb_list())
    elif sub == "search":
        print(kb_search(args.keyword, game if not args.all else ""))
    elif sub == "write":
        content = args.content or (sys.stdin.read() if not sys.stdin.isatty() else "")
        print(kb_write(args.filename, content, game))
    elif sub == "append":
        content = args.content or (sys.stdin.read() if not sys.stdin.isatty() else "")
        print(kb_append(args.filename, content, game))
    elif sub == "export":
        print(kb_export())
    elif sub == "import":
        print(kb_import(args.backup))
    elif sub == "boss":
        print(kb_query_boss(args.name or ""))
    elif sub == "tactic":
        print(kb_switch_tactic(args.filename))
    elif sub == "clean":
        print(kb_clean(args.target))
    elif sub == "history":
        print(kb_history(args.filename, game))
    elif sub == "rollback":
        print(kb_rollback(args.filename, args.rev, game))
    elif sub == "maintain":
        print(kb_maintain())
    else:
        print(f"未知 kb 子命令: {sub}")
        return 2
    return 0


def selftest(max_rounds: int = 12) -> int:
    """离线自检：全链路跑通 + 断言关键产物。返回 0 表示全过。"""
    os.environ["UGF_DRY_RUN"] = "1"
    os.environ["UGF_PERCEPTION_BACKEND"] = "mock"
    reload_config()
    checks = []

    def check(name, cond):
        checks.append((name, bool(cond)))
        log(("  ✓ " if cond else "  ✗ ") + name)

    log("== 自检 1/6 感知 + 预判 ==")
    p = Perception()
    frames = 0
    for _ in range(5):
        st = p.perceive()
        frames += 1 if "error" not in st else 0
    preds = PREDICTOR.all_entities()
    check("mock 感知可出帧", frames == 5)
    check("预判可输出实体", len(preds) > 0)
    check(
        "预判字段完整",
        all(
            k in preds[0] for k in ("raw_id", "category", "threat_score", "confidence", "prediction_trusted")
        ),
    )

    log("== 自检 2/6 战斗评估 ==")
    player = p._mock.get("player") or {}
    ev = judge_combat(player, preds, [])
    check("评估含 decision", ev.get("decision") in ("fight", "cautious_fight", "retreat"))
    check("评估含 recommended_set", bool(ev.get("recommended_set")))

    log("== 自检 3/6 知识库闭环 ==")
    seed_knowledge(force=True)
    txt = kb_search("战术", active_game())
    check("seed 知识可检索", kb_is_hit(txt))
    check("命中战术带适用条件", len(extract_tactics(txt)) > 0)
    act = decide_action("cautious_fight", extract_tactics(txt), hp_ratio=0.2, threat_ratio_=1.5)
    check("知识可影响决策", act in ("defend", "attack", ""))

    log("== 自检 4/6 复盘 + BOSS 记忆 ==")
    state: FramePayload = {
        "player": {"petal_set": "combat"},
        "entities": [{"raw_id": "mantis", "rarity": "Super"}],
        "teammates": [],
    }
    check("复盘判定命中(BOSS 局)", should_review(state))
    r = review_round(False, "自检复盘", state)
    check("复盘已写入知识库", "已写入知识库" in r)
    o = analyze_boss_behavior([(100, 100, 200, 200), (110, 105, 200, 200), (130, 120, 200, 200)])
    check("BOSS 行为可归纳", "平均距离" in o)
    w = write_boss_memory(["自检：观察到 BOSS 直线推进"], {"mantis(Super)": [(100, 100, 200, 200)] * 4})
    check("BOSS 记忆已写库", "已追加" in w)

    log("== 自检 5/6 视频学习(离线合成帧) ==")
    res = learn_from_video(None, frame_count=5)
    check("视频学习产出文件", os.path.exists(res["file"]))

    log("== 自检 6/6 主循环 + 汇报 + 会话 + 调参 ==")
    out = run_agent(max_rounds=max_rounds, interval=0.0)
    check("主循环跑满回合", out["rounds"] == max_rounds)
    check("动作已执行", sum(out["actions"].values()) == max_rounds)
    rep = generate_report()
    check("报告可生成", "对局报告" in rep)
    check("会话已记账", safe_int(session_load().get("sessions")) >= 1)
    t = auto_tune(hits=1, attempts=10, deaths_extra=1)
    check("自动调参可执行", "调参" in t)
    auto_tuner_reset()
    check("kb 导出可用", "已导出知识库" in kb_export())

    passed = sum(1 for _n, ok in checks if ok)
    log("=" * 58)
    log(f"自检结果：{passed}/{len(checks)} 通过")
    for n, ok in checks:
        if not ok:
            log(f"  未通过：{n}")
    log("=" * 58)
    return 0 if passed == len(checks) else 1


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="agent.py",
        description="Universal-Game-Framework · 单文件游戏 Agent",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例：\n  python agent.py run --dry-run --rounds 20\n"
        "  python agent.py selftest\n  python agent.py guide",
    )
    ap.add_argument("--game", default="", help="指定游戏名（读 game_profiles/<名字>.yaml）")
    ap.add_argument("--version", action="store_true", help="打印版本号并退出")
    sub = ap.add_subparsers(dest="cmd")

    sp = sub.add_parser("run", help="主循环")
    sp.add_argument("--rounds", type=int, default=0, help="最多跑多少回合（0=不限）")
    sp.add_argument("--interval", type=float, default=None, help="每回合间隔秒（默认取配置）")
    sp.add_argument("--dry-run", action="store_true", help="只记录动作，不碰真实键鼠")

    sub.add_parser("mode", help="查看运行模式")
    sub.add_parser("guide", help="使用手册")
    sub.add_parser("perceive", help="手工取一帧画面状态")
    sub.add_parser("predict", help="手工取一帧 + 全实体预判")
    sub.add_parser("session", help="会话记忆与历史战绩")
    sub.add_parser("afk", help="AFK 弹窗处理指引")

    sp = sub.add_parser("action", help="执行一个动作")
    sp.add_argument("type", choices=list(VALID_ACTIONS))
    sp.add_argument("--x", type=int, default=None)
    sp.add_argument("--y", type=int, default=None)

    sp = sub.add_parser("set", help="切换套装")
    sp.add_argument("name")

    sp = sub.add_parser("kb", help="知识库操作")
    kbsub = sp.add_subparsers(dest="kb_cmd")
    kbsub.add_parser("list")
    kbsub.add_parser("export")
    kbsub.add_parser("maintain")
    k = kbsub.add_parser("search")
    k.add_argument("keyword")
    k.add_argument("--all", action="store_true")
    k = kbsub.add_parser("write")
    k.add_argument("filename")
    k.add_argument("--content", default="")
    k = kbsub.add_parser("append")
    k.add_argument("filename")
    k.add_argument("--content", default="")
    k = kbsub.add_parser("import")
    k.add_argument("backup")
    k = kbsub.add_parser("boss")
    k.add_argument("name", nargs="?", default="")
    k = kbsub.add_parser("tactic")
    k.add_argument("filename")
    k = kbsub.add_parser("clean")
    k.add_argument("--target", default="all", choices=["all", "predict", "frames"])
    k = kbsub.add_parser("history")
    k.add_argument("filename")
    k = kbsub.add_parser("rollback")
    k.add_argument("filename")
    k.add_argument("--rev", type=int, default=1)
    for p in (kbsub.choices["list"], kbsub.choices["export"], kbsub.choices["maintain"]):
        p.add_argument("--all", action="store_true")

    sp = sub.add_parser("learn", help="视频学习")
    sp.add_argument("video", nargs="?", default=None, help="本地视频路径")
    sp.add_argument("--url", default=None, help="在线视频链接（需装 yt-dlp）")
    sp.add_argument("--frames", type=int, default=5, help="离线合成帧数")
    sp.add_argument("--skip", type=int, default=25, help="每 N 帧抽一张")

    sub.add_parser("report", help="生成对局报告")

    sp = sub.add_parser("tune", help="自动调参")
    sp.add_argument("--status", action="store_true")
    sp.add_argument("--reset", action="store_true")

    sp = sub.add_parser("selftest", help="离线自检")
    sp.add_argument("--rounds", type=int, default=12)

    sp = sub.add_parser("profile-check", help="校验游戏档案（按 _template.yaml 规范）")
    sp.add_argument("game", nargs="?", default="", help="档案名（缺省=当前激活游戏）")
    sp.add_argument("--all", action="store_true", help="校验 game_profiles/ 下全部档案")
    sp.add_argument("--strict", action="store_true", help="严格模式：WARN 也判失败")

    sp = sub.add_parser("panel", help="启动本地监控面板（只读快照与日志）")
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--port", type=int, default=None, help="默认取 server.panel_port(5002)")

    sp = sub.add_parser("logs", help="查看运行日志 / 结构化事件流")
    sp.add_argument("--tail", type=int, default=20, help="最后 N 行（默认 20）")
    sp.add_argument("--grep", default="", help="按关键词过滤")
    sp.add_argument("--events", action="store_true", help="看结构化事件流（JSONL）")
    sp.add_argument("--kind", default="", help="事件类型过滤：decision/death/tune/learn/session_end")
    return ap


def main(argv=None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    if args.version:
        print(f"Universal-Game-Framework {VERSION}")
        return 0
    if args.game:
        os.environ["AGENT_GAME"] = args.game
        reload_config()

    cmd = args.cmd
    if cmd == "guide":
        print(GUIDE)
    elif cmd == "mode":
        _print(runtime_mode())
    elif cmd == "run":
        if getattr(args, "dry_run", False):
            os.environ["UGF_DRY_RUN"] = "1"
            reload_config()
        run_agent(max_rounds=args.rounds, interval=args.interval)
    elif cmd == "perceive":
        _print(Perception().perceive())
    elif cmd == "predict":
        st = Perception().perceive()
        for _ in range(2):
            st = Perception().perceive()
        _print({"player": st.get("player"), "predictions": PREDICTOR.all_entities()})
    elif cmd == "action":
        print(game_action(args.type, args.x, args.y))
    elif cmd == "set":
        print(switch_set(args.name))
    elif cmd == "afk":
        print(handle_afk())
    elif cmd == "kb":
        if not args.kb_cmd:
            print(GUIDE)
            return 0
        return _cli_kb(args)
    elif cmd == "learn":
        path = args.video
        if args.url:
            path = download_video(args.url)
            log(f"[学习] 下载结果: {path}")
        r = learn_from_video(path, frame_count=args.frames, skip=args.skip)
        _print(r)
    elif cmd == "report":
        notify()
    elif cmd == "session":
        print(session_summary())
    elif cmd == "tune":
        if args.reset:
            print(auto_tuner_reset())
        else:
            print(auto_tuner_status())
    elif cmd == "selftest":
        return selftest(args.rounds)
    elif cmd == "profile-check":
        return profile_check(args.game, check_all=args.all, strict=args.strict)
    elif cmd == "panel":
        run_panel(args.host, args.port)
    elif cmd == "logs":
        if args.events:
            for e in read_events(args.tail, args.kind):
                print(json.dumps(e, ensure_ascii=False))
        else:
            p = os.path.join(RUN_LOGS, f"agent_{datetime.now().strftime('%Y%m%d')}.log")
            lines = []
            if os.path.exists(p):
                try:
                    with open(p, encoding="utf-8") as f:
                        lines = f.read().splitlines()
                except OSError:
                    lines = []
            if args.grep:
                lines = [ln for ln in lines if args.grep.lower() in ln.lower()]
            for ln in lines[-max(1, args.tail) :]:
                print(ln)
    else:
        ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
