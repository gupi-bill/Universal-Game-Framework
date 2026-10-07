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


