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


def llm_decide(state: FramePayload, predictions: list, combat_eval: dict, kb_text: str) -> ActionDict:
    """LLM 决策（ROADMAP #5/#8）：传输层重试 + schema 校验 + 一次修复重试 + 规则兜底。

    - prompt 可被游戏档案 llm.system_prompt 覆盖
    - 输出不合法时把校验错误回喂 LLM 修复一次，仍不合法才落 fallback_decide
    - 返回值恒带 source 标签：llm / kb / rule
    """
    url = (os.getenv("LLM_API_URL") or "").strip()
    key = (os.getenv("LLM_API_KEY") or "").strip()
    if not cfg_get("llm.use_ai", True):
        return fallback_decide(state, combat_eval, kb_text)
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


def fallback_decide(state: FramePayload, combat_eval: dict, kb_text: str = "") -> ActionDict:
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


