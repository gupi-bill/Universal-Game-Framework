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


