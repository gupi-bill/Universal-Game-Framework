"""ROADMAP #20：故障注入与边界测试——外部依赖全坏时主链路不崩、降级留痕。"""

import os
import sys


sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import agent  # noqa: E402


# ---------------------------------------------------------------------------
# 感知故障
# ---------------------------------------------------------------------------
def test_run_agent_survives_perception_outage(monkeypatch):
    """感知服务全程报错：主循环应记录、跳过并正常收尾，不崩溃。"""

    class DeadPerception(agent.Perception):
        def perceive(self):
            return {"error": "感知服务不可用: ConnectionError"}

    monkeypatch.setattr(agent, "Perception", DeadPerception)
    out = agent.run_agent(max_rounds=2, interval=0.0)
    assert out["rounds"] == 2
    assert sum(out["actions"].values()) == 0, "感知故障回合不应产生动作"


def test_perception_payload_with_garbage_entities():
    """实体列表混入 None/字符串/NaN 坐标：预判器应静默过滤。"""
    p = agent.Predictor()
    p.update(
        [
            None,
            "garbage",
            {},
            {"raw_id": "x", "x": float("nan"), "y": 1},
            {"raw_id": "ok", "rarity": "Common", "x": 10, "y": 10},
        ]
    )
    ents = p.all_entities()
    assert all(e["raw_id"] == "ok" for e in ents)


# ---------------------------------------------------------------------------
# LLM / Webhook 故障
# ---------------------------------------------------------------------------
def test_llm_transport_failure_falls_back(monkeypatch):
    import types

    mod = types.ModuleType("requests")

    def post(*a, **k):
        raise ConnectionError("LLM 服务炸了")

    mod.post = post
    monkeypatch.setitem(sys.modules, "requests", mod)
    monkeypatch.setenv("LLM_API_URL", "http://fake")
    monkeypatch.setenv("LLM_API_KEY", "k")
    monkeypatch.setattr(agent, "_CFG", {"resilience": {"llm": {"retries": 1, "backoff": 0, "timeout": 1}}})
    out = agent.llm_decide(
        {"player": {"hp": 100, "max_hp": 100}}, [], {"decision": "fight", "threat_ratio": 0.1}, ""
    )
    assert out["source"] in ("rule", "kb") and out["action"] in agent.VALID_ACTIONS


def test_webhook_unconfigured_notify_ok(monkeypatch, tmp_path):
    monkeypatch.delenv("UGF_WEBHOOK_URL", raising=False)
    monkeypatch.setattr(agent, "RUN_LOGS", str(tmp_path))
    monkeypatch.setattr(agent, "SNAP_FILE", str(tmp_path / "snap.json"))
    actions = agent.notify(quiet=True)
    assert any("未配置 Webhook" in a for a in actions)


# ---------------------------------------------------------------------------
# 知识库 / 配置文件损坏
# ---------------------------------------------------------------------------
def test_kb_search_skips_binary_garbage(tmp_path, monkeypatch):
    monkeypatch.setattr(agent, "KB_DIR", str(tmp_path))
    (tmp_path / "good.md").write_text("战术：低血量撤退", encoding="utf-8")
    with open(tmp_path / "bad.md", "wb") as f:
        f.write(b"\xff\xfe\x00\x01\x80\x81 not utf-8")
    out = agent.kb_search("战术")
    assert "good.md" in out and agent.kb_is_hit(out)


def test_corrupt_tuned_yaml_auto_tune_ok(tmp_path, monkeypatch):
    tp = tmp_path / "tuned_overrides.yaml"
    tp.write_text("{{{ not: [valid: yaml", encoding="utf-8")
    monkeypatch.setattr(agent, "TUNED_PATH", str(tp))
    msg = agent.auto_tune(hits=1, attempts=10, deaths_extra=1)
    assert "调参" in msg or "锁定" in msg


def test_corrupt_snapshot_report_ok(tmp_path, monkeypatch):
    snap = tmp_path / "agent_snapshot.json"
    snap.write_text("[broken", encoding="utf-8")
    monkeypatch.setattr(agent, "SNAP_FILE", str(snap))
    monkeypatch.setattr(agent, "RUN_LOGS", str(tmp_path))
    rep = agent.generate_report()
    assert "对局报告" in rep


def test_corrupt_history_read(tmp_path, monkeypatch):
    hf = tmp_path / "session_history.json"
    hf.write_text("not json at all", encoding="utf-8")
    monkeypatch.setattr(agent, "HISTORY_FILE", str(hf))
    assert agent._read_history() == []
    agent._history_append({"at": "x", "game": "g", "rounds": 1, "deaths": 0})
    assert len(agent._read_history("g")) == 1


def test_corrupt_config_yaml_defaults_survive(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("::: broken ::: yaml [[", encoding="utf-8")
    monkeypatch.setattr(agent, "CONFIG_PATH", str(cfg))
    agent.reload_config()
    assert agent.cfg_get("game.name") == "florr"
    assert agent.cfg_get("combat.retreat_ratio") is not None


# ---------------------------------------------------------------------------
# 磁盘写失败
# ---------------------------------------------------------------------------
def test_log_survives_unwritable_run_logs(tmp_path, monkeypatch, capsys):
    blocker = tmp_path / "run_logs"
    blocker.write_text("我是文件不是目录", encoding="utf-8")
    monkeypatch.setattr(agent, "RUN_LOGS", str(blocker))
    agent.log("这条日志不应导致崩溃")  # makedirs 会抛 NotADirectoryError/FileExistsError
    assert "这条日志不应导致崩溃" in capsys.readouterr().out


def test_dryrun_log_survives_unwritable(tmp_path, monkeypatch):
    blocker = tmp_path / "run_logs"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setattr(agent, "RUN_LOGS", str(blocker))
    monkeypatch.setenv("UGF_DRY_RUN", "1")
    out = agent.game_action("attack")
    assert "dry-run" in out


# ---------------------------------------------------------------------------
# 战斗评估边界
# ---------------------------------------------------------------------------
def test_judge_combat_degenerate_inputs():
    ev = agent.judge_combat({}, [], [])
    assert ev["decision"] in ("fight", "cautious_fight", "retreat")
    ev = agent.judge_combat(
        {"hp": 0, "max_hp": 100, "power_score": 0}, [{"category": "boss", "threat_score": 400}], []
    )
    assert ev["decision"] == "retreat", "零战力对 BOSS 必须撤"
    ev = agent.judge_combat(
        {"hp": -5, "max_hp": 0, "power_score": -100},
        [{"category": "normal", "threat_score": float("nan")}],
        [],
    )
    assert ev["decision"] in ("fight", "cautious_fight", "retreat")


def test_clamp_degenerate_margin():
    x, y = agent.clamp_to_safe_zone(50, 50, screen_w=100, screen_h=100, margin=200)
    assert (x, y) == (50.0, 50.0), "margin 超过尺寸时应回中心而非崩溃"


def test_threat_ratio_zero_power():
    assert agent.threat_ratio(0, 100) == 999.0


# ---------------------------------------------------------------------------
# 动作 / 战术边界
# ---------------------------------------------------------------------------
def test_game_action_invalid(monkeypatch):
    monkeypatch.setenv("UGF_DRY_RUN", "1")
    assert "未知动作类型" in agent.game_action("backflip")
    assert "必须提供 x 和 y" in agent.game_action("move")


def test_switch_set_unknown():
    assert "未知套装" in agent.switch_set("golden_armor")


def test_extract_tactics_garbage():
    assert agent.extract_tactics("") == []
    assert agent.extract_tactics("未找到相关内容") == []
    assert agent.extract_tactics(None) == []
    assert agent.extract_tactics("\x00\x01binary junk") == []


def test_kb_write_path_traversal_sanitized(tmp_path, monkeypatch):
    """路径穿越必须被消毒或拒绝，绝不能写出知识库目录之外。"""
    monkeypatch.setattr(agent, "KB_DIR", str(tmp_path))
    out = agent.kb_write("../../evil", "x", "g")
    assert not (tmp_path.parent / "evil.md").exists(), "不得逃逸出知识库目录"
    assert not (tmp_path / "evil.md").exists(), "不得落在 KB 根目录之外围"
    # safe_name 消毒后写在 KB 分区内，或明确拒绝——两者都算安全
    assert ("已写入" in out and (tmp_path / "g" / "evil.md").exists()) or "错误" in out


def test_learn_from_video_broken_path_falls_back(tmp_path, monkeypatch):
    monkeypatch.setattr(agent, "FRAME_DIR", str(tmp_path / "frames"))
    monkeypatch.setattr(agent, "KB_DIR", str(tmp_path / "kb"))
    res = agent.learn_from_video(str(tmp_path / "no_such_video.mp4"), frame_count=3)
    assert os.path.exists(res["file"]), "坏视频路径应降级到合成帧而不是崩溃"
