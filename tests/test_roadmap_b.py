"""ROADMAP 批次 B 回归测试：#18 状态版本化 / #16 日志 / #5 降级链 / #8 LLM 结构化。"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import agent  # noqa: E402


# ---------------------------------------------------------------------------
# #18 状态文件 schema 版本化
# ---------------------------------------------------------------------------
def test_state_v0_auto_migrates(tmp_path, monkeypatch):
    """老版本（无 schema_version）的 agent_state.json 能无感迁移。"""
    sf = tmp_path / "agent_state.json"
    sf.write_text(json.dumps({"game": "florr", "sessions": 3, "total_deaths": 7}), encoding="utf-8")
    monkeypatch.setattr(agent, "STATE_FILE", str(sf))
    st = agent.session_load()
    assert st["sessions"] == 3 and st["total_deaths"] == 7
    agent.session_save(st)
    saved = json.loads(sf.read_text(encoding="utf-8"))
    assert saved["schema_version"] == agent.STATE_SCHEMA_VERSION


def test_history_legacy_list_and_new_dict(tmp_path, monkeypatch):
    """旧纯列表战绩可读；追加后落盘为新 dict 格式且保留旧记录。"""
    hf = tmp_path / "session_history.json"
    hf.write_text(json.dumps([{"at": "x", "game": "florr", "rounds": 5, "deaths": 1}]), encoding="utf-8")
    monkeypatch.setattr(agent, "HISTORY_FILE", str(hf))
    assert len(agent._read_history()) == 1
    agent._history_append({"at": "y", "game": "florr", "rounds": 2, "deaths": 0})
    raw = json.loads(hf.read_text(encoding="utf-8"))
    assert raw["schema_version"] == agent.STATE_SCHEMA_VERSION
    assert len(raw["records"]) == 2
    # 新格式再次读取
    assert len(agent._read_history()) == 2


def test_corrupt_state_falls_back_to_default(tmp_path, monkeypatch):
    sf = tmp_path / "agent_state.json"
    sf.write_text("{broken json!!", encoding="utf-8")
    monkeypatch.setattr(agent, "STATE_FILE", str(sf))
    st = agent.session_load()
    assert st["status"] == "idle" and st["sessions"] == 0


# ---------------------------------------------------------------------------
# #16 日志轮转 + 结构化事件 + logs 命令
# ---------------------------------------------------------------------------
def test_log_rotation_compresses_and_purges(tmp_path, monkeypatch):
    import gzip
    import time as _time
    from datetime import datetime as _dt

    monkeypatch.setattr(agent, "RUN_LOGS", str(tmp_path))
    monkeypatch.setattr(agent, "_CFG", {"logs": {"max_size_mb": 0.0001, "retention_days": 7}})
    cur = tmp_path / f"agent_{_dt.now().strftime('%Y%m%d')}.log"
    cur.write_text("x" * 500, encoding="utf-8")  # >0.0001MB → 应压缩轮转
    old = tmp_path / "agent_20200101.log"
    old.write_text("old", encoding="utf-8")
    os.utime(old, (_time.time() - 30 * 86400, _time.time() - 30 * 86400))  # 超期 → 应删除

    agent._rotate_logs_if_needed(force=True)

    gzs = list(tmp_path.glob("*.gz"))
    assert gzs, "超大日志应被 gzip 轮转"
    with gzip.open(gzs[0], "rt", encoding="utf-8") as f:
        assert "xxxx" in f.read()
    assert len(cur.read_text(encoding="utf-8")) == 0, "轮转后原文件应清空续写"
    assert not old.exists(), "超过 retention_days 的日志应被清除"


def test_log_event_writes_valid_jsonl(tmp_path, monkeypatch):
    monkeypatch.setattr(agent, "RUN_LOGS", str(tmp_path))
    agent.log_event("decision", round=1, action="attack", source="rule")
    agent.log_event("death", round=2, deaths=1)
    evs = agent.read_events(10)
    assert [e["kind"] for e in evs] == ["decision", "death"]
    assert evs[0]["action"] == "attack"
    assert agent.read_events(10, kind="death")[-1]["round"] == 2


def test_logs_cli_exit_zero(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(agent, "RUN_LOGS", str(tmp_path))
    agent.log_event("learn", frames=3, kept=1)
    assert agent.main(["logs", "--events", "--tail", "5"]) == 0
    out = capsys.readouterr().out
    assert '"kind": "learn"' in out
    assert agent.main(["logs", "--tail", "5", "--grep", "nothing"]) == 0
