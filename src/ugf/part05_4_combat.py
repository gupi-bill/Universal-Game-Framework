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


