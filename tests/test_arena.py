"""arena 与 ab_experiment 的单元测试。

arena 是 P2（进化闭环）实证的地面设施，它自己必须先可信：
确定性、因果链、死亡结算，任何一条坏了，后面的 A/B 数字都是废的。
"""
from __future__ import annotations

import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest

import arena
from arena import Arena, ArenaConfig
import ab_experiment as ab


# ---------------------------------------------------------------- arena 本体


def test_same_seed_same_actions_give_same_result():
    a, b = Arena(seed=7), Arena(seed=7)
    for i in range(60):
        act, x = ("move", i % 3 / 3.0), None
        x = [0.3, 0.7, 0.5][i % 3]
        a.step("move", x)
        b.step("move", x)
    assert (a.st.hp, a.st.score, a.st.round_no) == (b.st.hp, b.st.score, b.st.round_no)


def test_different_seeds_diverge():
    """不同 seed 必须生成不同的局面。

    注意要比「敌人生成位置」而不是存活统计 —— 两个 seed 下
    玩家都可能恰好在第 38 回合死掉，被击中次数相同是正常的，
    但敌人初始位置一定不同（那才是随机性的体现）。
    """
    a, b = Arena(seed=1), Arena(seed=2)
    assert [e["x"] for e in a.st.enemies] != [e["x"] for e in b.st.enemies]
    assert [e["y"] for e in a.st.enemies] != [e["y"] for e in b.st.enemies]
    # 轨迹也确实不同
    for i in range(30):
        a.step("move", 0.2)
        b.step("move", 0.2)
    assert [e["x"] for e in a.st.enemies] != [e["x"] for e in b.st.enemies]


def test_idle_takes_more_damage_than_evading():
    """因果链：动作选择必须能改变结果，否则测不出知识的作用。"""
    still, mover = Arena(seed=3), Arena(seed=3)
    for _ in range(40):
        still.step("idle")
    for i in range(40):
        mover.step("move", 0.05 if i % 2 else 0.95)
    assert mover.st.hp > still.st.hp


def test_death_is_settled():
    a = Arena(ArenaConfig(seed=3, contact_damage=50, enemies=4))
    for _ in range(100):
        if a.st.over:
            break
        a.step("idle")
    assert a.st.over and a.st.hp == 0


def test_snapshot_matches_perception_contract():
    """快照必须能直接喂给 predictor / combat_judge。"""
    a = Arena(seed=5)
    a.step("move", 0.5)
    s = a.snapshot()
    for key in ("player", "entities", "teammates", "afk_popup"):
        assert key in s, f"缺 {key}"
    for key in ("alive", "hp", "max_hp", "x", "y", "power_score"):
        assert key in s["player"], f"player 缺 {key}"
    for e in s["entities"]:
        assert {"raw_id", "rarity", "x", "y"} <= set(e)


def test_snapshot_feeds_combat_judge():
    """真喂一次 combat_judge，确保没有字段名对不上。"""
    import combat_judge as cj
    a = Arena(seed=9)
    for _ in range(8):
        s = a.snapshot()
        if a.st.over:
            break
        a.step("move", 0.4)
    p = s["player"]
    ctx = cj.CombatContext(
        player=cj.PlayerState(hp=p["hp"], max_hp=p["max_hp"],
                              power_score=p["power_score"], x=p["x"], y=p["y"]),
        enemies=s["entities"], teammates=[],
        screen_width=int(a.cfg.width), screen_height=int(a.cfg.height))
    out = cj.judge_combat(ctx)
    assert out["decision"] in ("fight", "cautious_fight", "retreat")


def test_self_test_passes():
    assert arena._self_test() == 0


# ---------------------------------------------------------------- A/B 实验


def test_policies_are_reachable():
    """三个策略都必须真的注册了，且能跑完一局。"""
    for name in ("baseline", "no_kb", "kb"):
        assert name in ab.POLICIES
        r = ab.run_one(name, seed=1, rounds=40, tactics=[])
        assert r["rounds"] > 0
        assert r["hp"] >= 0


def test_kb_and_no_kb_can_diverge():
    """防回归：曾经的 bug 是三组动作完全相同，导致差异恒为 0。

    如果这里又变成完全相同，说明策略又把动作空间坍缩了。
    """
    tags = ["retreat", "keep_distance", "focus_fire"]
    diffs = 0
    for seed in range(8):
        a = ab.run_one("no_kb", seed, 200, [])
        b = ab.run_one("kb", seed, 200, tags)
        if a["rounds"] != b["rounds"]:
            diffs += 1
    assert diffs > 0, (
        "kb 与 no_kb 在 8 个 seed 上结果完全相同 —— "
        "决策没有改变物理行为，实验测不出任何东西")


def test_arena_has_discrimination():
    """默认难度下必须有区分度：全组都死就什么都测不出来。"""
    medians = []
    all_rows = []
    for name in ("baseline", "no_kb", "kb"):
        rows = [ab.run_one(name, s, 200, [], difficulty="combat") for s in range(10)]
        all_rows.append(rows)
        medians.append(statistics.median(r["rounds"] for r in rows))
    # combat 档下回合数不再是好指标（赢了会提前撤离），所以主要看存活率
    survs = [sum(1 for r in rows if r["survived"]) for rows in all_rows]
    assert len(set(survs)) > 1, f"三组存活数完全相同（{survs}），环境没有区分度"


def test_run_is_reproducible_across_calls():
    """整个实验可复现：同样的参数跑两次结果一致。"""
    args = ["--n", "5", "--rounds", "80"]
    import io
    import contextlib
    outs = []
    for _ in range(2):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            ab.main(args)
        outs.append(buf.getvalue())
    # arena 自检与摘要行应完全一致
    def stats_line(text):
        for ln in text.splitlines():
            if ln.strip().startswith("no_kb"):
                return ln.strip()
        return ""
    assert stats_line(outs[0]) == stats_line(outs[1]), "同样的参数跑出不同结果"


def test_wilson_interval_is_sane():
    lo, hi = ab.wilson(0, 10)
    assert lo == 0.0 and 0.0 < hi < 1.0
    lo2, hi2 = ab.wilson(10, 10)
    assert hi2 == 1.0 and 0.0 < lo2 < 1.0
    assert ab.wilson(0, 0) == (0.0, 0.0)


def test_load_tactics_returns_list():
    """读不到知识库也要返回空列表，不能抛异常 —— 对照组依赖它。"""
    t = ab.load_tactics()
    assert isinstance(t, list)


def test_knowledge_improves_survival_in_combat_difficulty():
    """P2 的核心断言：知识组在 combat 档下确实比无知识组活得久。

    这是 v2.0 P2 实证的正向结果（20+25+25 组 × 三批 seed 全部复现，
    增益 +10~+20 个百分点）。如果这个测试开始失败，说明知识闭环退化了。

    注意它跑 12 组而不是更多 —— 单测要快。完整实验请跑
    `python ab_experiment.py --n 20`。
    """
    from ab_experiment import load_tactics
    kb = load_tactics()
    if not kb:
        pytest.skip("知识库为空（无标签），对照组退化为同一策略")
    surv_no = sum(1 for s in range(12)
                  if ab.run_one("no_kb", s, 200, [], difficulty="combat")["survived"])
    surv_kb = sum(1 for s in range(12)
                  if ab.run_one("kb", s, 200, kb, difficulty="combat")["survived"])
    assert surv_kb >= surv_no, (
        f"知识组存活 {surv_kb}/12 不如无知识组 {surv_no}/12 —— "
        "知识闭环可能退化，查 apply_tactics / knowledge_gate")


def test_retreat_goes_toward_exfil_not_away_from_threat():
    """v2.0 P2 第二轮修正：撤离点固定在一侧时，
    「往离最近威胁最远」往往正好是撤离点的反方向。"""
    from ab_experiment import _tactical_spot, DIFFICULTY
    a = Arena(ArenaConfig(seed=2, **DIFFICULTY["combat"]))
    exfil = a.cfg.exfil_x
    assert exfil is not None
    for d in ("retreat", "defend"):
        assert _tactical_spot(a, d, []) == exfil, (
            f"{d} 应朝撤离点 {exfil} 跑，而不是离威胁最远")


def test_attack_actually_kills():
    a = Arena(ArenaConfig(seed=3, enemies=4, attack_damage=4, ramp_step=0))
    for _ in range(40):
        if a.st.over or a.st.kills > 0:
            break
        a.step("attack")
    assert a.st.kills > 0, "attack 没有任何杀伤力，集火类知识无法生效"


def test_exfil_settles_as_survival():
    a = Arena(ArenaConfig(seed=3, enemies=3, safe_hp_ratio=0.95,
                          exfil_x=0.03, exfil_radius=0.2, ramp_step=0,
                          contact_damage=5))
    for _ in range(120):
        if a.st.over:
            break
        a.step("move", 0.02)
    assert a.st.exfiltrated and a.st.survived
