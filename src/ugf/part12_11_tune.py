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


