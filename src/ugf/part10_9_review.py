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


