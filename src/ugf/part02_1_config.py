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
    "agent.checkpoint_interval": 30,  # ROADMAP v2 #6：每 N 回合落盘循环检查点（0=关）
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


