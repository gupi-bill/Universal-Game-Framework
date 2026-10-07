# ===========================================================================
# 8. 会话记忆 & 断点续玩
# ===========================================================================
STATE_FILE = os.path.join(BASE_DIR, "agent_state.json")
HISTORY_FILE = os.path.join(BASE_DIR, "session_history.json")
SNAP_FILE = os.path.join(RUN_LOGS, "agent_snapshot.json")

# ROADMAP #18：状态文件结构版本化——读取时自动迁移，写入时打版本号，
# 未来改结构不会弄坏老用户的断点续玩与历史战绩。
STATE_SCHEMA_VERSION = 2


def _migrate_state_file(raw) -> dict:
    """状态文件容器迁移（ROADMAP v2 #5）：v0/v1 平铺单游戏 → v2 按游戏分区。

    v2 形态：{"schema_version": 2, "games": {<game>: {...状态字段...}}}
    旧平铺数据归入其 game 字段所指游戏（缺省归当前激活游戏），零丢失。
    """
    if not isinstance(raw, dict):
        return {"schema_version": STATE_SCHEMA_VERSION, "games": {}}
    if safe_int(raw.get("schema_version")) >= 2 and isinstance(raw.get("games"), dict):
        return raw
    g = safe_name(str(raw.get("game") or active_game())) or "default"
    flat = {k: v for k, v in raw.items() if k != "schema_version"}
    return {"schema_version": STATE_SCHEMA_VERSION, "games": {g: flat}}


def _read_state_container() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:
        return {"schema_version": STATE_SCHEMA_VERSION, "games": {}}
    return _migrate_state_file(raw)


def session_load(game: str = "") -> dict:
    g = safe_name(game or active_game()) or "default"
    default = {
        "game": g,
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
    st = _read_state_container().get("games", {}).get(g)
    if not isinstance(st, dict):
        return default
    default.update(st)
    default["game"] = g
    return default


def session_save(state: dict, game: str = ""):
    g = safe_name(game or state.get("game") or active_game()) or "default"
    container = _read_state_container()
    games = container.setdefault("games", {})
    st = dict(state)
    st.pop("schema_version", None)
    st["game"] = g
    games[g] = st
    container["schema_version"] = STATE_SCHEMA_VERSION
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(container, f, ensure_ascii=False, indent=2)
    except (OSError, TypeError, ValueError) as e:
        raise RuntimeError(f"会话档案保存失败: {e}") from e


def session_record_start(game: str) -> bool:
    """开玩前调用；返回是否可「续玩」。"""
    st = session_load(game)
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
    session_save(st, game)
    return resumable


def session_record_end(game: str, rounds: int, deaths: int, report: str = ""):
    st = session_load(game)
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
    session_save(st, game)
    _history_append({"at": st["last_played"], "game": game, "rounds": rounds, "deaths": deaths})


def _migrate_history_file(raw) -> dict:
    """战绩容器迁移：v0 纯列表 / v1 {"records":[]} → v2 按游戏分区。"""
    if (
        isinstance(raw, dict)
        and safe_int(raw.get("schema_version")) >= 2
        and isinstance(raw.get("games"), dict)
    ):
        return raw
    recs = []
    if isinstance(raw, list):
        recs = [r for r in raw if isinstance(r, dict)]
    elif isinstance(raw, dict) and isinstance(raw.get("records"), list):
        recs = [r for r in raw["records"] if isinstance(r, dict)]
    games: dict = {}
    for r in recs:
        g = safe_name(str(r.get("game") or "")) or "default"
        games.setdefault(g, []).append(r)
    return {"schema_version": STATE_SCHEMA_VERSION, "games": games}


def _read_history_container() -> dict:
    return _migrate_history_file(_read_json(HISTORY_FILE, []))


def _read_history(game: str = "") -> list:
    """某游戏的战绩记录（缺省当前游戏）。"""
    g = safe_name(game or active_game()) or "default"
    recs = _read_history_container().get("games", {}).get(g)
    return [r for r in recs if isinstance(r, dict)] if isinstance(recs, list) else []


def _history_all() -> list:
    """全部游戏战绩，按时间排序。"""
    out: list = []
    for recs in _read_history_container().get("games", {}).values():
        out.extend(r for r in recs if isinstance(r, dict))
    out.sort(key=lambda r: str(r.get("at") or ""))
    return out


def _history_append(record: dict):
    container = _read_history_container()
    g = safe_name(str(record.get("game") or "")) or "default"
    games = container.setdefault("games", {})
    recs = games.setdefault(g, [])
    recs.append(record)
    games[g] = recs[-100:]
    container["schema_version"] = STATE_SCHEMA_VERSION
    try:
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(container, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


def session_summary(game: str = "", all_games: bool = False) -> str:
    if all_games:
        played = _history_all()
        st = {
            "sessions": len(played),
            "total_deaths": sum(safe_int(r.get("deaths")) for r in played),
            "last_played": max((str(r.get("at") or "") for r in played), default=""),
            "last_rounds": 0,
        }
        head = "会话(全部游戏汇总)"
    else:
        g = safe_name(game or active_game()) or "default"
        st = session_load(g)
        played = _read_history(g)
        head = f"会话({g})"
    total_rounds = sum(safe_int(r.get("rounds")) for r in played)
    total_deaths = sum(safe_int(r.get("deaths")) for r in played)
    line = (
        f"{head}：玩过 {safe_int(st.get('sessions'))} 场 ｜ 本次/上次回合 {safe_int(st.get('last_rounds'))} "
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


