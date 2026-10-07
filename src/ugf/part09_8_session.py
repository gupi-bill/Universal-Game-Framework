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


