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


# ---------------------------------------------------------------------------
# #5 统一降级链（故障注入）
# ---------------------------------------------------------------------------
class _FakeRequests:
    """可编程 requests 替身：get/post 按脚本抛错或返回。"""

    def __init__(self):
        self.get_calls = 0
        self.post_calls = 0
        self.get_script = None
        self.post_script = None

    def get(self, url, timeout=None):
        self.get_calls += 1
        return self.get_script(self.get_calls)

    def post(self, url, headers=None, json=None, timeout=None):
        self.post_calls += 1
        return self.post_script(self.post_calls, json)


_FAST_CFG = {
    "resilience": {
        "perception": {"retries": 2, "backoff": 0},
        "webhook": {"retries": 1, "backoff": 0, "timeout": 1},
        "llm": {"retries": 1, "backoff": 0, "timeout": 1},
    },
    "perception": {"timeout": 1},
}


def _use_http_perception(monkeypatch, fake):
    monkeypatch.setitem(sys.modules, "requests", fake)
    monkeypatch.setenv("UGF_PERCEPTION_BACKEND", "http")
    monkeypatch.setenv("UGF_PERCEPTION_URL", "http://127.0.0.1:1/perceive")
    monkeypatch.setattr(agent, "_CFG", dict(_FAST_CFG))


def test_perception_http_retries_then_errors(monkeypatch):
    fake = _FakeRequests()

    def boom(i):
        raise ConnectionError("unreachable")

    fake.get_script = boom
    _use_http_perception(monkeypatch, fake)
    frame = agent.Perception()._http_frame()
    assert "error" in frame and "unreachable" in frame["error"]
    assert fake.get_calls == 3, "首次 + 2 次重试"


def test_perception_http_retry_recovers(monkeypatch):
    fake = _FakeRequests()

    class R:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"player": {"alive": True}, "entities": [], "_raw": 1}

    def script(i):
        if i < 2:
            raise ConnectionError("瞬断")
        return R()

    fake.get_script = script
    _use_http_perception(monkeypatch, fake)
    frame = agent.Perception()._http_frame()
    assert "error" not in frame and frame["player"]["alive"] is True
    assert "_raw" not in frame


def test_webhook_retries_then_fails(monkeypatch):
    fake = _FakeRequests()

    def boom(i, payload):
        raise ConnectionError("网络挂了")

    fake.post_script = boom
    monkeypatch.setitem(sys.modules, "requests", fake)
    monkeypatch.setenv("UGF_WEBHOOK_URL", "http://127.0.0.1:1/hook")
    monkeypatch.setattr(agent, "_CFG", dict(_FAST_CFG))
    ok, why = agent.push_webhook("测试")
    assert ok is False and "网络挂了" in why
    assert fake.post_calls == 2, "首次 + 1 次重试"


# ---------------------------------------------------------------------------
# #8 LLM 决策结构化（schema 校验 + 修复重试 + 来源标签）
# ---------------------------------------------------------------------------
def test_validate_action_schema():
    ok, err = agent._validate_action({"action": "attack"})
    assert ok == {"action": "attack", "source": "llm"} and err == ""
    ok, err = agent._validate_action({"action": "move", "x": 10.7, "y": "20"})
    assert ok == {"action": "move", "x": 10, "y": 20, "source": "llm"}
    bad, err = agent._validate_action({"action": "fly"})
    assert bad is None and "action" in err
    bad, err = agent._validate_action({"action": "move", "x": True, "y": 3})
    assert bad is None, "bool 冒充数字应被拒绝"
    bad, err = agent._validate_action(["not", "dict"])
    assert bad is None
    ok, _ = agent._validate_action({"action": "idle", "junk": 1})
    assert "junk" not in ok, "白名单外字段应被丢弃"


def _fake_llm_requests(monkeypatch, responses):
    """按调用次序返回预设 LLM 内容的 requests 替身。"""
    fake = _FakeRequests()
    calls = []

    class R:
        status_code = 200

        def __init__(self, content):
            self._c = content

        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"content": self._c}}]}

    def post(i, payload):
        calls.append(payload)
        return R(responses[min(i - 1, len(responses) - 1)])

    fake.post_script = post
    monkeypatch.setitem(sys.modules, "requests", fake)
    monkeypatch.setenv("LLM_API_URL", "http://fake/llm")
    monkeypatch.setenv("LLM_API_KEY", "k")
    monkeypatch.setenv("LLM_MODEL", "m")
    monkeypatch.setattr(agent, "_CFG", dict(_FAST_CFG))
    return calls


def test_llm_invalid_then_repair_retry(monkeypatch):
    calls = _fake_llm_requests(
        monkeypatch,
        [
            "抱歉我说不出 JSON",
            '{"action":"attack"}',
        ],
    )
    out = agent.llm_decide({"player": {}}, [], {"decision": "fight"}, "")
    assert out["action"] == "attack" and out["source"] == "llm"
    assert len(calls) == 2, "应发生一次修复重试"
    repair_msgs = calls[1]["messages"]
    assert any("不合法" in str(m.get("content")) for m in repair_msgs if m["role"] == "user")


def test_llm_all_invalid_falls_back_to_rule(monkeypatch):
    _fake_llm_requests(monkeypatch, ["垃圾输出", "还是垃圾"])
    out = agent.llm_decide(
        {"player": {"hp": 100, "max_hp": 100}}, [], {"decision": "fight", "threat_ratio": 0.1}, ""
    )
    assert out["source"] in ("rule", "kb")
    assert out["action"] in agent.VALID_ACTIONS


def test_llm_json_fence_parsed(monkeypatch):
    _fake_llm_requests(monkeypatch, ['```json\n{"action":"move","x":1,"y":2}\n```'])
    out = agent.llm_decide({"player": {}}, [], {"decision": "fight"}, "")
    assert out == {"action": "move", "x": 1, "y": 2, "source": "llm"}


def test_fallback_tags_source():
    out = agent.fallback_decide({"player": {"hp": 100, "max_hp": 100}, "afk_popup": True}, {})
    assert out == {"action": "idle", "source": "rule"}
