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


