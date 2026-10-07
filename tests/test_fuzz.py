"""ROADMAP v2 #15：随机脏数据 fuzz——全链路不崩、输出契约完整、数值有限。

固定随机种子保证可复现；覆盖 预判链 / 战斗评估链 / 配置极端值 三组性质。
"""

import math
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import agent  # noqa: E402

_JUNK = [None, float("nan"), float("inf"), float("-inf"), -50, "120", True, [], {}]


def _rand_coord(rng):
    if rng.random() < 0.2:
        return rng.choice(_JUNK)
    return rng.uniform(-200, 3000)


def test_predictor_fuzz_200_sequences():
    """200 组随机实体序列喂预判器：不抛异常，输出契约完整，数值有限。"""
    rng = random.Random(20261007)
    for _seq in range(200):
        p = agent.Predictor()
        ids = [rng.choice(["a", "b", "a", "enemy_player", "ally_x"]) for _ in range(rng.randint(0, 6))]
        for _frame in range(rng.randint(1, 12)):
            ents = []
            for rid in ids:
                if rng.random() < 0.1:
                    ents.append(rng.choice([None, "junk", 42]))
                    continue
                e = {"raw_id": rid, "x": _rand_coord(rng), "y": _rand_coord(rng)}
                if rng.random() < 0.8:
                    e["rarity"] = rng.choice(["Common", "Epic", "Super", "Unique", "", None, 123])
                if rng.random() < 0.3:
                    e["role"] = rng.choice(["player_enemy", "player_ally", "monster"])
                ents.append(e)
            p.update(ents)
        for out in p.all_entities():
            for key in (
                "raw_id",
                "category",
                "threat_score",
                "confidence",
                "prediction_trusted",
                "x_now",
                "y_now",
                "model",
            ):
                assert key in out, f"契约字段缺失: {key}"
            assert isinstance(out["confidence"], float) and 0.0 <= out["confidence"] <= 1.0
            for k in ("x_now", "y_now", "vx_per_sec", "vy_per_sec"):
                assert math.isfinite(out[k]), f"{k} 必须是有限数: {out[k]}"


def test_combat_chain_fuzz():
    """200 组脏玩家/敌人数据过评估→决策链：不崩、动作合法、来源合法。"""
    rng = random.Random(42)
    kb_texts = [
        "",
        "未找到相关内容",
        "- 战术: 低血量撤退（适用条件：血量低于四成）",
        "- 战术: 集火（适用条件：存在高威胁目标时）",
        "\x00\x01junk",
    ]
    for _ in range(200):
        player = {
            "hp": rng.choice([0, -3, 55, 100, None, "x", float("nan")]),
            "max_hp": rng.choice([0, 100, None, -1]),
            "power_score": rng.choice([0, -10, 120, None, "abc"]),
            "petal_set": rng.choice(["combat", "nope", None]),
        }
        ents = [
            {
                "category": rng.choice(
                    ["normal", "elite", "boss", "highest_boss", "player_enemy", "weird", None]
                ),
                "threat_score": rng.choice([0, 15, 400, None, -5]),
            }
            for _ in range(rng.randint(0, 5))
        ]
        ev = agent.judge_combat(player, ents, rng.choice([[], [{"petal_set": "tank"}]]))
        assert ev["decision"] in ("fight", "cautious_fight", "retreat")
        assert ev["recommended_set"]
        assert math.isfinite(ev["threat_ratio"])
        act = agent.fallback_decide(
            {"player": player, "entities": ents, "teammates": []}, ev, rng.choice(kb_texts)
        )
        assert act["action"] in agent.VALID_ACTIONS
        assert act["source"] in ("kb", "rule")


def test_config_extremes_fuzz(monkeypatch):
    """极端配置值下走位钳制/抖动/知识闸门不崩且输出有限。"""
    rng = random.Random(7)
    for _ in range(80):
        cfg = {
            "combat": {
                "safe_zone_w": rng.choice([0, -100, 1920, 10]),
                "safe_zone_h": rng.choice([0, 1080, 10]),
                "safe_zone_margin": rng.choice([0, 5000, 100, -20]),
                "jitter_base": rng.choice([-5, 0, 1e6]),
                "jitter_max": rng.choice([-1, 15, 1e6]),
                "retreat_ratio": rng.choice([0, -1, 1e9, 1.0]),
                "flee_distance": rng.choice([0, -300, 300]),
                "strafe_distance": rng.choice([0, 150]),
                "chase_max_distance": 400,
                "chase_min_category": "elite",
            },
            "predictor": {"threat": {}, "confidence_threshold": rng.choice([-1, 2, 0.65])},
        }
        monkeypatch.setattr(agent, "_CFG", cfg)
        x, y = agent.clamp_to_safe_zone(rng.uniform(-1e5, 1e5), rng.uniform(-1e5, 1e5))
        assert math.isfinite(x) and math.isfinite(y)
        jx, jy = agent.apply_jitter(500, 500)
        assert math.isfinite(jx) and math.isfinite(jy)
        out = agent.decide_action(
            rng.choice(["fight", "cautious_fight", "retreat"]),
            [("retreat", ("低血量",)), ("focus_fire", ("存在高威胁目标",))],
            hp_ratio=rng.choice([0.0, 0.1, 1.0]),
            threat_ratio_=rng.choice([0.0, 0.5, 2.0, 999.0]),
        )
        assert out in ("defend", "attack", "")


def test_validate_action_fuzz():
    """畸形 LLM 输出 200 组：要么合法动作要么明确报错，绝不半吊子。"""
    rng = random.Random(99)
    junk = [
        None,
        42,
        "attack",
        [],
        {"action": None},
        {"action": 7},
        {"action": "move"},
        {"action": "move", "x": "a", "y": 2},
        {"action": "move", "x": float("nan"), "y": 0},
        {"action": "MOVE", "x": 1.5, "y": 2.5},
        {"action": "idle", "junk": 1},
    ]
    for _ in range(200):
        cand = rng.choice(junk)
        out, err = agent._validate_action(cand)
        assert (out is None) != (err == ""), "合法与报错必须二选一"
        if out is not None:
            assert out["action"] in agent.VALID_ACTIONS and out["source"] == "llm"
            if out["action"] == "move":
                assert math.isfinite(out["x"]) and math.isfinite(out["y"])
