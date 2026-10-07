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
            elif self.path.startswith("/healthz"):
                # ROADMAP v2 #4：容器编排/负载均衡标准探针端点
                body = json.dumps(
                    {
                        "status": "ok",
                        "version": VERSION,
                        "ts": datetime.now().isoformat(timespec="seconds"),
                    }
                ).encode("utf-8")
                self._send(200, "application/json; charset=utf-8", body)
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
# 14b. 诊断与基准（ROADMAP v2 #1/#2/#3）
# ===========================================================================
def _dep_report() -> list:
    """可选依赖体检：(模块名, 解锁能力, 是否已装)。"""
    deps = [
        ("yaml", "config/游戏档案加载（缺失只用内置默认值）"),
        ("requests", "LLM 决策 / VLM 学习 / Webhook / http 感知"),
        ("pyautogui", "真实键鼠控制（仅非 dry-run 需要）"),
        ("cv2", "template 后端 / local 预处理 / 视频抽帧"),
        ("mss", "local / template 后端抓屏"),
        ("numpy", "local / template 后端数值处理"),
        ("onnxruntime", "local 后端 ONNX 推理"),
    ]
    out = []
    for mod, why in deps:
        try:
            __import__(mod)
            out.append((mod, why, True))
        except ImportError:
            out.append((mod, why, False))
    return out


def doctor(game: str = "") -> int:
    """ROADMAP v2 #1：环境一站式体检。致命项（✗）存在时返回 1。"""
    g = safe_name(game or active_game()) or active_game()
    fatal = 0
    warns = 0

    def ok(msg):
        print(f"  ✓ {msg}")

    def warn(msg):
        nonlocal warns
        warns += 1
        print(f"  ⚠ {msg}")

    def bad(msg):
        nonlocal fatal
        fatal += 1
        print(f"  ✗ {msg}")

    print(f"== UGF Doctor · v{VERSION} · 游戏={g} · {runtime_mode_text()} ==")

    print("[Python]")
    v = sys.version_info
    if (v.major, v.minor) >= (3, 10):
        ok(f"Python {v.major}.{v.minor}.{v.micro}")
    else:
        bad(f"需要 Python ≥ 3.10，当前 {v.major}.{v.minor}")

    print("[依赖]（✗=缺失但可降级，不致命）")
    for mod, why, has in _dep_report():
        (ok if has else warn)(f"{mod}" + ("" if has else " 未安装") + f" — {why}")

    print("[运行目录]")
    try:
        os.makedirs(BASE_DIR, exist_ok=True)
        probe = os.path.join(BASE_DIR, ".ugf_doctor_probe")
        with open(probe, "w", encoding="utf-8") as f:
            f.write("x")
        os.remove(probe)
        ok(f"BASE_DIR 可写: {BASE_DIR}")
    except OSError as e:
        bad(f"BASE_DIR 不可写: {e}")

    print("[游戏档案]")
    issues = profile_check_one(g)
    errs = [i for i in issues if i[0] == "ERROR"]
    wrs = [i for i in issues if i[0] == "WARN"]
    if errs:
        bad(f"档案 {g}: {len(errs)} 个 ERROR（profile-check {g} 查看全部）")
    elif wrs:
        warn(f"档案 {g}: {len(wrs)} 个警告（profile-check {g} 查看全部）")
    else:
        ok(f"档案 {g} 校验通过")

    print("[感知]")
    p = Perception()
    b = p.backend()
    if b == "http":
        url = (os.getenv("UGF_PERCEPTION_URL") or "").strip() or str(cfg_get("perception.http_url", "") or "")
        if not url:
            port = safe_int(cfg_get("server.perception_port", 5001), 5001)
            url = f"http://127.0.0.1:{port}/perceive"
        try:
            import requests

            r = requests.get(url, timeout=3)
            ok(f"http 感知服务可达（HTTP {r.status_code}）: {url}")
        except Exception as e:
            warn(f"http 感知服务不可达: {type(e).__name__}（只跑 dry-run 可忽略）")
    elif b == "local":
        mp = str(cfg_get("perception.local.model_path", "") or "")
        if mp and os.path.exists(mp):
            ok(f"local 模型存在: {mp}")
        else:
            bad(f"local 后端模型缺失: {mp or '未配置 perception.local.model_path'}")
    elif b == "template":
        td = str(cfg_get("perception.template.dir", "") or "")
        if td and os.path.isdir(td):
            ok(f"template 模板目录存在: {td}")
        else:
            bad(f"template 后端目录缺失: {td or '未配置 perception.template.dir'}")
    else:
        ok("mock 后端（离线合成，无需外部服务）")

    print("[外部服务配置]（只查有无，不显示值）")
    for name, u, k in (("LLM", "LLM_API_URL", "LLM_API_KEY"), ("VLM", "VLM_API_URL", "VLM_API_KEY")):
        if (os.getenv(u) or "").strip() and (os.getenv(k) or "").strip():
            ok(f"{name} 已配置（URL + KEY）")
        else:
            warn(f"{name} 未配置（{'决策走规则兜底' if name == 'LLM' else '视频学习跳过 VLM'}）")
    wh = (os.getenv("UGF_WEBHOOK_URL") or "").strip() or str(cfg_get("agent.webhook_url", "") or "")
    if wh:
        ok("Webhook 已配置")
    else:
        warn("Webhook 未配置（报告仅写本地文件）")

    print(f"== 体检结果：{'通过' if fatal == 0 else f'{fatal} 个致命项'} ｜ 警告 {warns} 条 ==")
    return 1 if fatal else 0


def brief(game: str = "") -> int:
    """ROADMAP v2 #7：开局侦察报告——档案/知识库/战绩/调参一屏聚合。"""
    g = safe_name(game or active_game()) or active_game()
    print(f"== UGF Brief · {g} · v{VERSION} ==")
    print(f"[模式] {runtime_mode_text()}")

    prof = _read_yaml(os.path.join(PROFILE_DIR, f"{g}.yaml"))
    if prof:
        desc = str((prof.get("game") or {}).get("description") or "")
        pred = prof.get("predictor") or {}
        combat = prof.get("combat") or {}
        tiers = "/".join(
            str(len(pred.get(k) or []))
            for k in ("rarity_highest_boss", "rarity_boss", "rarity_elite", "rarity_normal")
        )
        issues = profile_check_one(g)
        n_err = sum(1 for i in issues if i[0] == "ERROR")
        n_warn = sum(1 for i in issues if i[0] == "WARN")
        print(f"[档案] {desc[:50] or '(无描述)'}")
        print(f"       稀有度档数(最高/BOSS/精英/普通)={tiers} ｜ 套装={combat.get('sets')}")
        print(f"       默认套装={combat.get('default_set')} ｜ 战术 {len(combat.get('tactics') or [])} 条")
        verdict = "✓ 通过" if not n_err else f"✗ {n_err} 错误"
        print(f"       档案校验：{verdict}" + (f" / {n_warn} 警告" if n_warn else ""))
    else:
        print(f"[档案] game_profiles/{g}.yaml 不存在，使用内置默认（接入新游戏见 _template.yaml）")

    d = kb_game_dir(g)
    if os.path.isdir(d):
        files = [f for f in os.listdir(d) if f.endswith(".md")]
        hist_dir = os.path.join(KB_DIR, KB_HISTORY_DIRNAME)
        n_hist = 0
        if os.path.isdir(hist_dir):
            for hf in os.listdir(hist_dir):
                if not hf.endswith(".jsonl"):
                    continue
                try:
                    with open(os.path.join(hist_dir, hf), encoding="utf-8") as f:
                        n_hist += sum(1 for line in f if line.strip())
                except OSError:
                    pass
        print(f"[知识库] {len(files)} 篇 / {_dir_mb(d):.2f} MB ｜ 历史修订 {n_hist} 条")
    else:
        print("[知识库] （空 — 首次运行会自动补种 seed 知识）")

    print("[战绩] " + session_summary(g).replace("\n", "\n       "))
    print("[调参] " + auto_tuner_status().replace("\n", "\n       "))
    return 0


def bench(rounds: int = 200) -> int:
    """ROADMAP v2 #2：分段耗时基准（强制 mock + dry-run，规则决策，零外部依赖）。

    度量 感知/预判/评估/决策/动作 五段的 avg/p50/max 与整体吞吐，
    结果写一条 kind=bench 事件进 events.jsonl，供跨版本对比防性能回退。
    """
    import statistics

    os.environ["UGF_DRY_RUN"] = "1"
    os.environ["UGF_PERCEPTION_BACKEND"] = "mock"
    reload_config()
    n = max(1, safe_int(rounds, 200))
    perception = Perception()
    evaluator = CombatEvaluator()
    seg: dict = {"perceive": [], "predict": [], "evaluate": [], "decide": [], "act": []}
    t_all = time.perf_counter()
    for _i in range(n):
        t0 = time.perf_counter()
        state = perception.perceive()
        t1 = time.perf_counter()
        preds = PREDICTOR.all_entities()
        t2 = time.perf_counter()
        _praw = state.get("player")
        player = _praw if isinstance(_praw, dict) else {}
        ev = evaluator.evaluate(player, preds, state.get("teammates") or [])
        t3 = time.perf_counter()
        action = fallback_decide(state, ev, "")
        t4 = time.perf_counter()
        atype = str(action.get("action", "idle"))
        if atype == "move":
            sx, sy = scale_coords(action.get("x", 400), action.get("y", 300))
            tx, ty = apply_jitter(*clamp_to_safe_zone(sx, sy))
            game_action("move", int(tx), int(ty))
        else:
            game_action(atype)
        t5 = time.perf_counter()
        seg["perceive"].append(t1 - t0)
        seg["predict"].append(t2 - t1)
        seg["evaluate"].append(t3 - t2)
        seg["decide"].append(t4 - t3)
        seg["act"].append(t5 - t4)
    total = time.perf_counter() - t_all
    print(f"== bench · {n} 回合 · 总耗时 {total:.2f}s · 吞吐 {n / total:.1f} 回合/s ==")
    print(f"{'段':<10}{'avg(ms)':>10}{'p50(ms)':>10}{'max(ms)':>10}")
    avg_ms = {}
    for k, vals in seg.items():
        ms = [x * 1000 for x in vals]
        avg_ms[k] = round(statistics.mean(ms), 3)
        print(f"{k:<10}{avg_ms[k]:>10.3f}{statistics.median(ms):>10.3f}{max(ms):>10.3f}")
    log_event("bench", rounds=n, total_s=round(total, 3), avg_ms=avg_ms)
    return 0


def event_stats(kind: str = "") -> str:
    """ROADMAP v2 #3：events.jsonl 聚合统计，一屏看清跑成什么样。"""
    from collections import Counter

    evs = read_events(10**9, kind)
    if not evs:
        return "暂无事件记录（先跑一局：python agent.py run --dry-run --rounds 20）"
    decisions = [e for e in evs if e.get("kind") == "decision"]
    acts = Counter(e.get("action") for e in decisions if e.get("action"))
    srcs = Counter(str(e.get("source") or "?") for e in decisions)
    n_death = sum(1 for e in evs if e.get("kind") == "death")
    n_dec = len(decisions)
    n_tune = sum(1 for e in evs if e.get("kind") == "tune")
    learns = [e for e in evs if e.get("kind") == "learn"]
    n_kept = sum(safe_int(e.get("kept")) for e in learns)
    sess = [e for e in evs if e.get("kind") == "session_end"]
    total_rounds = sum(safe_int(e.get("rounds")) for e in sess)
    lines = [f"== 事件流统计（共 {len(evs)} 条{'，kind=' + kind if kind else ''}）=="]
    rate = (n_death / n_dec * 100) if n_dec else 0.0
    lines.append(f"决策回合 {n_dec} ｜ 死亡 {n_death} ｜ 死亡率 {rate:.1f}%")
    if acts:
        lines.append("动作分布: " + ", ".join(f"{k}×{v}" for k, v in acts.most_common()))
    if srcs:
        lines.append("决策来源: " + ", ".join(f"{k}×{v}" for k, v in srcs.most_common()))
    lines.append(
        f"调参 {n_tune} 次 ｜ 视频学习 {len(learns)} 次（入库 {n_kept} 条）"
        f" ｜ 会话 {len(sess)} 个（合计 {total_rounds} 回合）"
    )
    benches = [e for e in evs if e.get("kind") == "bench"]
    if benches:
        b = benches[-1]
        lines.append(f"最近 bench: {b.get('rounds')} 回合 / {b.get('total_s')}s / avg_ms={b.get('avg_ms')}")
    return "\n".join(lines)


def replay(tail: int = 0) -> str:
    """ROADMAP v2 #12：基于 events.jsonl 的对局回放（文本时间线）。

    默认取「上一个 session_end 之后」的最近一局窗口；死亡回合前后标注 ☠。
    """
    evs = read_events(10**9)
    if not evs:
        return "暂无事件记录（先跑一局：python agent.py run --dry-run --rounds 20）"
    ends = [i for i, e in enumerate(evs) if e.get("kind") == "session_end"]
    start = ends[-2] + 1 if len(ends) >= 2 else 0
    window = evs[start:]
    if safe_int(tail) > 0:
        window = window[-safe_int(tail) :]
    death_rounds = {safe_int(e.get("round")) for e in window if e.get("kind") == "death"}
    lines = [f"== 对局回放（最近一局 · 事件 {len(window)} 条）=="]
    for e in window:
        kind = e.get("kind")
        ts = str(e.get("ts", ""))[-8:]
        if kind == "decision":
            r = safe_int(e.get("round"))
            near_death = any((r + d) in death_rounds for d in (-1, 0, 1))
            mark = "☠" if near_death else " "
            lines.append(
                f"{mark} {ts} R{r:<5} {str(e.get('action', '?')):<9} "
                f"来源={str(e.get('source') or '?'):<7} 决策={e.get('decision', '?')} 心态={e.get('mindset', '?')}"
            )
        elif kind == "death":
            lines.append(f"☠ {ts} R{safe_int(e.get('round'))} 死亡（累计 {safe_int(e.get('deaths'))}）")
        elif kind == "tune":
            lines.append(f"⚙ {ts} {e.get('detail', '')}")
        elif kind == "learn":
            lines.append(f"📚 {ts} 视频学习：{e.get('frames')} 帧 → 入库 {e.get('kept')} 条")
        elif kind == "session_end":
            lines.append(
                f"🏁 {ts} 收局：回合={e.get('rounds')} 死亡={e.get('deaths')} 耗时={e.get('elapsed')}s"
            )
    return "\n".join(lines)


