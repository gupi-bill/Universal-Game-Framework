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
    sf.write_text(json.dumps({"game": "florr", "sessions": 3, "total_deaths": 7}),
                  encoding="utf-8")
    monkeypatch.setattr(agent, "STATE_FILE", str(sf))
    st = agent.session_load()
    assert st["sessions"] == 3 and st["total_deaths"] == 7
    agent.session_save(st)
    saved = json.loads(sf.read_text(encoding="utf-8"))
    assert saved["schema_version"] == agent.STATE_SCHEMA_VERSION


def test_history_legacy_list_and_new_dict(tmp_path, monkeypatch):
    """旧纯列表战绩可读；追加后落盘为新 dict 格式且保留旧记录。"""
    hf = tmp_path / "session_history.json"
    hf.write_text(json.dumps([{"at": "x", "game": "florr", "rounds": 5, "deaths": 1}]),
                  encoding="utf-8")
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
