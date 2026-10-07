"""ROADMAP v2 回归测试。"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import agent  # noqa: E402


# ---------------------------------------------------------------------------
# v2#1 doctor
# ---------------------------------------------------------------------------
def test_doctor_exit_zero_in_offline_env(capsys, monkeypatch):
    """离线环境（mock+dry-run）体检应无致命项。"""
    monkeypatch.delenv("AGENT_GAME", raising=False)
    monkeypatch.delenv("UGF_GAME", raising=False)
    assert agent.main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "体检结果" in out and "Python" in out


def test_doctor_bad_game_is_fatal(capsys):
    assert agent.main(["doctor", "no_such_game_xyz"]) == 1
    assert "致命项" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# v2#2 bench
# ---------------------------------------------------------------------------
def test_bench_runs_and_logs_event(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(agent, "RUN_LOGS", str(tmp_path))
    monkeypatch.setattr(agent, "SNAP_FILE", str(tmp_path / "snap.json"))
    assert agent.main(["bench", "--rounds", "5"]) == 0
    out = capsys.readouterr().out
    for seg in ("perceive", "predict", "evaluate", "decide", "act"):
        assert seg in out
    evs = [e for e in agent.read_events(100) if e.get("kind") == "bench"]
    assert evs and evs[-1]["rounds"] == 5


# ---------------------------------------------------------------------------
# v2#3 logs --stats
# ---------------------------------------------------------------------------
def test_event_stats_aggregation(tmp_path, monkeypatch):
    monkeypatch.setattr(agent, "RUN_LOGS", str(tmp_path))
    agent.log_event("decision", action="attack", source="rule")
    agent.log_event("decision", action="attack", source="rule")
    agent.log_event("decision", action="move", source="llm")
    agent.log_event("death", round=3, deaths=1)
    agent.log_event("session_end", rounds=10, deaths=1)
    s = agent.event_stats()
    assert "决策回合 3" in s and "死亡 1" in s
    assert "attack×2" in s and "llm×1" in s
    assert "合计 10 回合" in s
    assert "暂无事件" in agent.event_stats(kind="nonexistent")


def test_logs_stats_cli(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(agent, "RUN_LOGS", str(tmp_path))
    agent.log_event("decision", action="idle", source="kb")
    assert agent.main(["logs", "--stats"]) == 0
    assert "事件流统计" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# v2#4 healthz
# ---------------------------------------------------------------------------
def test_panel_healthz(tmp_path, monkeypatch):
    import threading
    import urllib.request

    monkeypatch.setattr(agent, "RUN_LOGS", str(tmp_path))
    monkeypatch.setattr(agent, "SNAP_FILE", str(tmp_path / "snap.json"))
    srv, port = agent.start_panel_server("127.0.0.1", 0)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=5) as r:
            assert r.status == 200
            data = json.loads(r.read().decode("utf-8"))
        assert data["status"] == "ok" and data["version"] == agent.VERSION
    finally:
        srv.shutdown()
        srv.server_close()
