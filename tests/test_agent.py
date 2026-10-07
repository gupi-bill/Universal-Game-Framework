"""单文件 Agent 核心能力回归测试。

只测 agent.py 这个文件，覆盖：配置 / 知识闭环 / 预判 / 战斗评估 / 复盘 /
汇报 / 调参 / CLI。全部离线运行（mock 感知 + dry-run 动作）。
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import agent  # noqa: E402


# ---------------------------------------------------------------------------
# 配置 / 工具函数
# ---------------------------------------------------------------------------
def test_safe_helpers_tolerate_dirty_data():
    assert agent.safe_float(None) == 0.0
    assert agent.safe_float("abc", 1.5) == 1.5
    assert agent.safe_float(float("nan"), 2.0) == 2.0
    assert agent.safe_int("7") == 7
    assert agent.safe_int(True, 9) == 9


def test_safe_name_blocks_path_traversal():
    assert "/" not in agent.safe_name("../../etc/passwd")
    assert ".." not in agent.safe_name("a..b/c")
    assert agent.safe_name("florr") == "florr"


def test_config_has_builtin_defaults():
    agent.reload_config()
    assert agent.cfg_get("game.name")
    assert agent.cfg_get("combat.retreat_ratio") is not None
    assert agent.active_game()


# ---------------------------------------------------------------------------
# 知识闭环
# ---------------------------------------------------------------------------
def test_seed_and_search_knowledge():
    agent.seed_knowledge(force=True)
    txt = agent.kb_search("战术", agent.active_game())
    assert agent.kb_is_hit(txt)
    assert "未找到" not in txt


def test_tactics_carry_conditions_and_match_state():
    line = "低血量时撤退，适用条件：低血量"
    parsed = agent.parse_condition(line)
    assert parsed["tag"] == "retreat"
    assert agent.condition_matches(parsed["conditions"], hp_ratio=0.2) is True
    assert agent.condition_matches(parsed["conditions"], hp_ratio=0.9) is False


def test_tactic_without_condition_is_ignored():
    assert agent.parse_condition("随便写点什么") == {}


def test_knowledge_gate_blocks_empty_field():
    tactics = [("retreat", ("低血量",))]
    assert agent.knowledge_gate("fight", tactics, threat_ratio_=0.0) is False
    assert agent.knowledge_gate("retreat", tactics, threat_ratio_=2.0) is True


def test_decide_action_uses_knowledge():
    tactics = [("retreat", ("低血量",))]
    assert agent.decide_action("cautious_fight", tactics, hp_ratio=0.2, threat_ratio_=1.5) in (
        "defend",
        "attack",
        "",
    )


# ---------------------------------------------------------------------------
# 预判
# ---------------------------------------------------------------------------
def test_predictor_tracks_and_predicts():
    p = agent.Predictor()
    for i in range(5):
        p.update([{"raw_id": "hornet", "rarity": "Common", "x": 100 + i * 40, "y": 200}])
    preds = p.all_entities()
    assert preds, "连续帧后应能预判出实体"
    top = preds[0]
    for key in ("raw_id", "category", "threat_score", "confidence", "prediction_trusted"):
        assert key in top


def test_predictor_needs_enough_frames():
    p = agent.Predictor()
    p.update([{"raw_id": "hornet", "rarity": "Common", "x": 100, "y": 200}])
    only = p.all_entities()[0]
    assert only["x_predict"] is None
    assert only["prediction_trusted"] is False


# ---------------------------------------------------------------------------
# 战斗评估
# ---------------------------------------------------------------------------
def test_threat_ratio_and_mindset():
    assert agent.threat_ratio(100, 300) > 1
    player = {"hp": 20, "max_hp": 100, "power_score": 50, "petal_set": "combat"}
    enemies = [{"raw_id": "mantis", "category": "boss", "threat_score": 400}]
    assert agent.decide_mindset(player, enemies) in ("aggressive", "conservative", "balanced")


def test_judge_combat_returns_decision_and_set():
    player = {"hp": 100, "max_hp": 100, "power_score": 500, "petal_set": "combat"}
    enemies = [{"raw_id": "hornet", "category": "normal", "threat_score": 15, "prediction_trusted": True}]
    ev = agent.judge_combat(player, enemies, [])
    assert ev["decision"] in ("fight", "cautious_fight", "retreat")
    assert ev["recommended_set"]


def test_clamp_to_safe_zone_and_jitter():
    x, y = agent.clamp_to_safe_zone(-50, 5000)
    assert 0 < x < 1920 and 0 < y < 1080
    jx, jy = agent.apply_jitter(100, 100)
    assert abs(jx - 100) <= agent.safe_int(agent.cfg_get("combat.jitter_max", 15), 15) + 1
    assert abs(jy - 100) <= agent.safe_int(agent.cfg_get("combat.jitter_max", 15), 15) + 1


# ---------------------------------------------------------------------------
# 感知（mock）+ 主循环产物
# ---------------------------------------------------------------------------
def test_mock_perception_feeds_predictor():
    agent.PREDICTOR.reset()
    p = agent.Perception()
    st = {}
    for _ in range(5):
        st = p.perceive()
    assert "error" not in st
    assert st["player"]["alive"] is True
    assert agent.PREDICTOR.all_entities()


def test_run_agent_dry_run_executes_actions():
    out = agent.run_agent(max_rounds=6, interval=0.0)
    assert out["rounds"] == 6
    assert sum(out["actions"].values()) == 6


# ---------------------------------------------------------------------------
# 复盘 / 汇报 / 会话 / 调参
# ---------------------------------------------------------------------------
def test_review_triggers_on_boss_round():
    state = {
        "player": {"petal_set": "combat"},
        "entities": [{"raw_id": "mantis", "rarity": "Super"}],
        "teammates": [],
    }
    assert agent.should_review(state) is True
    assert "已写入知识库" in agent.review_round(False, "测试复盘", state)


def test_boss_behavior_summary():
    samples = [(100, 100, 200, 200), (110, 105, 200, 200), (130, 120, 200, 200)]
    assert "平均距离" in agent.analyze_boss_behavior(samples)


def test_report_and_session():
    assert "对局报告" in agent.generate_report()
    agent.session_record_start(agent.active_game())
    agent.session_record_end(agent.active_game(), rounds=3, deaths=1)
    assert agent.session_summary()


def test_auto_tune_and_reset():
    agent.auto_tuner_reset()
    assert "调参" in agent.auto_tune(hits=1, attempts=10, deaths_extra=1)
    assert agent.auto_tuner_status()
    agent.auto_tuner_reset()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "argv",
    [
        ["mode"],
        ["kb", "list"],
        ["action", "attack"],
        ["set", "retreat"],
        ["session"],
        ["tune", "--status"],
    ],
)
def test_cli_subcommands_exit_zero(argv):
    assert agent.main(argv) == 0


def test_cli_run_dry_run(capsys):
    assert agent.main(["run", "--dry-run", "--rounds", "3"]) == 0
    out = capsys.readouterr().out
    assert "回合 3" in out
