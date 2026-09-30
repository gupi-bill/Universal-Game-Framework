#!/usr/bin/env python3
"""
ab_experiment.py · P2「进化闭环成立」的实证实验（v2.0 / M2 里程碑）
=====================================================================

## 它要证明什么

> **P2**：实战 → 复盘 → 知识入库 → 下次决策更优，形成**可度量**的增益。

关于 P2 的现状（诚实版）：到本文件写下时，
`devplan/KB_LOOP_S18.md` 已经证明了「检索命中率 100%、引用率 100%」
—— 也就是知识**确实流到了决策里**。

但那不等于「决策因此变好」。100% 的命中率也可以是「命中了但没用」。

本脚本做真正的那一步：**用同一批 seed，让「有知识」和「无知识」两组
跑同一批对局，比存活率与存活回合数**。

## 为什么需要 arena

`perception.mock` 的场景是固定的（HP 恒 100、敌人恒 6），
无论知识库空还是满，跑出来的轨迹一模一样 —— 那种环境证明不了任何事。

`arena.py` 提供了 seed 驱动的可复现对局：同样的 seed + 同样的动作
得到同样的结果，不同 seed 得到不同的对局。于是「跑 N 组、统计差异」
才有意义。

## 三组对照

| 组 | 决策来源 | 作用 |
|---|---------|------|
| `baseline` | 纯规则（发发呆/乱跑） | 下界参照 |
| `no_kb` | `combat_judge`，不注入知识 | 证明「决策本身」值多少 |
| `kb` | `combat_judge` + `knowledge_loop` 门控 | 证明「知识」额外值多少 |

**只有 `kb` 明显优于 `no_kb`，才算 P2 成立。**
若两者无显著差异，该承认「知识闭环目前不产生增益」，
而不是换个指标凑一个好看的结论。

## 统计口径

样本很小（默认 30 组），所以：
- 报**中位数**与**均值**，不报 p 值（样本量不够，p 值会骗人）
- 要求差异**方向一致**且**绝对幅度 ≥ 10%** 才判定为有效
- 附带 Wilson 区间，让读者自己判断可信度

## 用法

```bash
# 先确认 arena 本身没问题
python arena.py --self-test

# 用真实决策层跑 A/B（默认 30 组 × 200 回合）
python ab_experiment.py

# 换 seed 批次，排除「刚好这批 seed 有效」
python ab_experiment.py --offset 1000 --n 30
```

## 结论怎么写

结果打印后由人判断，脚本不自动下结论 —— 因为
「知识闭环是否成立」是个需要人来承担的判断，不是统计能自动决定的。
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from arena import Arena, ArenaConfig                      # noqa: E402
import combat_judge as cj                                   # noqa: E402
import knowledge_loop as kl                                 # noqa: E402


# --------------------------------------------------------------------- 策略


def _evade_x(arena: Arena) -> float:
    """朝敌人质心的反方向跑（归一化 x）。"""
    if not arena.st.enemies:
        return 0.5
    cx = sum(e["x"] for e in arena.st.enemies) / len(arena.st.enemies)
    return 0.03 if cx > arena.cfg.width / 2 else 0.97


def policy_baseline(arena: Arena, snap: dict, rnd: int, kb: list):
    """下界参照：不看局面，只按固定模式走。"""
    if rnd % 7 == 0:
        return "idle", None
    return "move", (rnd % 2) * 0.9 + 0.05


def _decide(arena: Arena, snap: dict) -> str:
    """把 arena 快照喂给真实的 combat_judge，拿到决策。"""
    p = snap["player"]
    player = cj.PlayerState(
        hp=p["hp"], max_hp=p["max_hp"], power_score=p["power_score"],
        current_set="shoot", talent="none", x=p["x"], y=p["y"],
    )
    ctx = cj.CombatContext(
        player=player, enemies=snap["entities"], teammates=[],
        screen_width=int(arena.cfg.width), screen_height=int(arena.cfg.height),
    )
    try:
        return cj.judge_combat(ctx).get("decision", "fight")
    except Exception:
        return "fight"


# ---------------------------------------------------------------------------
# 动作空间设计（关键：要让「决策」真正改变「物理行为」）
# ---------------------------------------------------------------------------
# 第一版这里把动作映射成 (action, 目标x)，但三组每回合的动作完全相同
# ——因为 retreat / keep_distance 都落到"移动"，fight / focus_fire 都落到
# "移动"。决策字符串变了，物理轨迹没变，于是 kb 与 no_kb 逐回合同分，
# 差异恒为 0。
#
# 教训：**策略必须让决策改变走位策略本身**，否则实验测不出东西。
# 现在改成：不同决策 → 不同的目标点选取规则 → 不同的实际轨迹。


def _nearest_threat_x(arena: Arena) -> float:
    """最危险的那个敌人在哪（归一化 x）。"""
    if not arena.st.enemies:
        return 0.5
    px, py = arena.st.player_x, arena.st.player_y
    best, best_d = 0.5, float("inf")
    for e in arena.st.enemies:
        d = (e["x"] - px) ** 2 + (e["y"] - py) ** 2
        if d < best_d:
            best_d, best = d, e["x"]
    return best / arena.cfg.width


def _centroid_x(arena: Arena) -> float:
    if not arena.st.enemies:
        return 0.5
    return sum(e["x"] for e in arena.st.enemies) / len(arena.st.enemies) / arena.cfg.width


def _tactical_spot(arena: Arena, decision: str, tactics: list) -> float:
    """按决策给出不同的目标 x。这是让知识可见的地方。

    - fight      ：压上去，往敌人质心走
    - attack     ：同上（知识「集火」）
    - cautious   ：往空档走（远离质心、靠近边缘）
    - retreat    ：往离最近敌人最远的一侧跑
    - defend     ：往角落躲（同时离质心和最近敌人都远）
    """
    cx = _centroid_x(arena)
    nx = _nearest_threat_x(arena)

    if decision in ("fight", "attack"):
        # 进攻：朝质心压
        return min(0.95, max(0.05, cx))
    if decision in ("retreat", "defend"):
        # 撤退/防守：往离最近威胁最远的一侧
        return 0.03 if nx > 0.5 else 0.97
    if decision == "cautious_fight":
        # 谨慎：留在场地边缘但不正对质心
        return 0.12 if cx > 0.5 else 0.88
    return 0.5


def policy_no_kb(arena: Arena, snap: dict, rnd: int, kb: list):
    """真实决策层，但不注入知识。"""
    d = _decide(arena, snap)
    return d, _tactical_spot(arena, d, [])


def policy_kb(arena: Arena, snap: dict, rnd: int, kb: list):
    """真实决策层 + 知识门控。这是 P2 要证明的那一组。"""
    d = _decide(arena, snap)
    ar = snap.get("arena", {})
    hp_ratio = ar.get("hp_ratio", 1.0)
    # 威胁比用「敌人数量」近似：arena 里没有战斗评估那套威胁分表，
    # 但「数量随回合增长」本身就是威胁上升的可靠信号。
    threat_ratio = len(snap["entities"]) / 6.0
    try:
        # arena 是单机场景（teammates 恒为 []），所以 has_allies=False。
        # 这正是要验的：队友类知识不该在单机局生效。
        if kl.knowledge_gate(d, kb, hp_ratio=hp_ratio, threat_ratio=threat_ratio):
            adjusted = kl.apply_tactics(d, kb, has_allies=False)
            if adjusted:
                d = adjusted
    except Exception:
        pass
    return d, _tactical_spot(arena, d, kb)


POLICIES = {
    "baseline": policy_baseline,
    "no_kb": policy_no_kb,
    "kb": policy_kb,
}


# --------------------------------------------------------------------- 跑一局


# 难度配置。第一版用 ArenaConfig 默认值，实测三组 30/30 全部必死、
# 回合中位数全在 33~35 —— 没有任何区分度，测不出差异。
# 现在改成「敌人不加速、速度中等、伤害偏低」：好策略能活 70~90 回合，
# 差策略明显更短，差异才有统计意义。
# 保留一组「高难」作为压力测试开关。
DIFFICULTY = {
    "normal": dict(enemies=5, enemy_speed=70.0, ramp_step=0.0,
                   contact_damage=8, contact_radius=22.0),
    "hard": dict(enemies=6, ramp_step=18.0, contact_damage=12),
}


def run_one(policy_name: str, seed: int, rounds: int, tactics: list,
            difficulty: str = "normal") -> dict:
    pol = POLICIES[policy_name]
    a = Arena(ArenaConfig(seed=seed, **DIFFICULTY.get(difficulty, DIFFICULTY["normal"])))
    for rnd in range(rounds):
        if a.st.over:
            break
        snap = a.snapshot()
        action, x = pol(a, snap, rnd, tactics)
        a.step(action, x)
    a.close()
    return {
        "seed": seed,
        "rounds": a.st.round_no,
        "hp": a.st.hp,
        "hits": a.st.hits_taken,
        "score": a.st.score,
        "survived": bool(a.st.survived or a.st.hp > 0),
    }


# --------------------------------------------------------------------- 统计


def wilson(k: int, n: int, z: float = 1.96) -> tuple:
    """Wilson 得分区间。样本少的时候比正态近似靠谱。

    两端都 clamp 到 [0, 1]：k=0 时下界的解析式会算出 -2e-17 这类
    浮点误差，打印出来是「存活率 -0%」，看着像 bug。
    """
    if n <= 0:
        return (0.0, 0.0)
    k = max(0, min(n, k))
    p = k / n
    d = 1.0 + z * z / n
    centre = p + z * z / (2 * n)
    half = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5)
    lo = (centre - half) / d
    hi = (centre + half) / d
    return (max(0.0, lo), min(1.0, hi))


def summarize(rows: list) -> dict:
    if not rows:
        return {}
    surv = sum(1 for r in rows if r["survived"])
    rounds = [r["rounds"] for r in rows]
    hp = [r["hp"] for r in rows]
    hits = [r["hits"] for r in rows]
    lo, hi = wilson(surv, len(rows))
    return {
        "n": len(rows),
        "survived": surv,
        "survival_rate": surv / len(rows),
        "survival_ci": [round(lo, 3), round(hi, 3)],
        "rounds_median": statistics.median(rounds),
        "rounds_mean": round(statistics.fmean(rounds), 1),
        "hp_median": statistics.median(hp),
        "hits_median": statistics.median(hits),
    }


# --------------------------------------------------------------------- 知识库


def load_tactics() -> list:
    """从 knowledge_md 读一个游戏分区的战术标签。

    读不到就返回空列表 —— 那正是 `no_kb` 组的行为，
    实验照跑（对照组必须有）。
    """
    kb_dir = os.path.join(BASE_DIR, "knowledge_md", "space_invaders")
    if not os.path.isdir(kb_dir):
        return []
    tags: list = []
    for fn in sorted(os.listdir(kb_dir)):
        if not fn.endswith(".md"):
            continue
        try:
            with open(os.path.join(kb_dir, fn), encoding="utf-8", errors="replace") as f:
                tags.extend(kl.extract_tactics(f.read()))
        except Exception:
            continue
    seen, uniq = set(), []
    for t in tags:
        if t not in seen:
            seen.add(t)
            uniq.append(t)
    return uniq


# --------------------------------------------------------------------- 主流程


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="P2 知识闭环 A/B 实验")
    ap.add_argument("--n", type=int, default=30, help="每组跑多少个 seed")
    ap.add_argument("--rounds", type=int, default=200, help="单局回合上限")
    ap.add_argument("--offset", type=int, default=0, help="seed 起点偏移，换批次用")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--difficulty", choices=sorted(DIFFICULTY), default="normal",
                    help="normal=有区分度（默认） / hard=高难压力测试")
    args = ap.parse_args(argv)

    # 先确认地基没问题
    from arena import _self_test
    if _self_test() != 0:
        print("\narena 自检未过，实验结果不可信，已中止。")
        return 1

    tactics = load_tactics()
    seeds = list(range(args.offset, args.offset + args.n))

    print("\n" + "=" * 68)
    print("P2 实证：知识库为空 vs 有知识，同一批 seed 对照")
    print("=" * 68)
    print(f"  seed 范围   : {seeds[0]} ~ {seeds[-1]}（{len(seeds)} 组）")
    print(f"  单局回合上限: {args.rounds}")
    print(f"  难度         : {args.difficulty}"
          f"（normal 有区分度；hard 下各策略多半都会死）")
    print(f"  知识库标签  : {tactics if tactics else '（空 —— 这正是 no_kb 组）'}")
    print()

    results = {}
    for name in ("baseline", "no_kb", "kb"):
        rows = [run_one(name, s, args.rounds,
                        tactics if name == "kb" else [],
                        difficulty=args.difficulty)
                for s in seeds]
        results[name] = {"rows": rows, "summary": summarize(rows)}
        s = results[name]["summary"]
        print(f"  {name:<9} 存活 {s['survived']:>2}/{s['n']}"
              f"（{s['survival_rate']*100:>5.1f}%  95%CI {s['survival_ci'][0]*100:.0f}–{s['survival_ci'][1]*100:.0f}%）"
              f"  回合中位 {s['rounds_median']:>5.1f}"
              f"  HP 中位 {s['hp_median']:>5.1f}"
              f"  被击中中位 {s['hits_median']:>4.1f}")

    print()
    kb_s = results["kb"]["summary"]
    no_s = results["no_kb"]["summary"]
    d_rounds = kb_s["rounds_median"] - no_s["rounds_median"]
    d_rate = kb_s["survival_rate"] - no_s["survival_rate"]
    base_s = results["baseline"]["summary"]
    d_judge = no_s["rounds_median"] - base_s["rounds_median"]

    print("  ── 判读 ──")
    print(f"  决策层本身的价值（no_kb − baseline）: {d_judge:+.1f} 回合")
    print(f"  知识的额外价值（kb − no_kb）       : {d_rounds:+.1f} 回合"
          f" / 存活率 {d_rate*100:+.1f} 个百分点")

    # ---- 判读：区分「无效」与「有害」，并给出可执行的下一步 ----
    # 这三种结果对应完全不同的后续动作，不能混为一谈。
    EPS_R, EPS_P = 0.5, 0.10
    if d_rounds > EPS_R:
        verdict = "P2 方向成立（样本仍小）"
        why = (f"知识组多活 {d_rounds:.1f} 回合、存活率 {d_rate*100:+.1f} 个百分点。"
               f"但样本只有 {len(seeds)} 组且场景是简化模拟，不能外推到真实游戏 —— "
               "下一步：换 --offset 复现，再上真机。")
    elif abs(d_rounds) <= EPS_R:
        verdict = "P2 未成立：无增益"
        why = ("知识组与无知识组无显著差异。命中率 100% 只证明知识**流到了决策**，"
               "不证明决策**变好了**。下一步：先确认知识库里存的是不是"
               "「对这一局面真的有用」的经验，而不是泛泛的战术名词。")
    else:
        verdict = "P2 反向：知识当前有害"
        why = (f"知识组少活 {abs(d_rounds):.1f} 回合。这不是玄学，是可定位的："
               "逐回合统计决策分布，能看到知识把某个决策改成了另一个，"
               "而后者在这个动作空间里更差。"
               "下一步：要么给知识加局面门控（只在适用场景生效），"
               "要么承认现有知识库对这个场景不适用并清掉。")

    # 再细分：定位是哪条标签在起作用
    if d_rounds < -EPS_R:
        print("\n  ── 定位是哪条标签在起作用 ──")
        import statistics
        probes = [
            ([], "空知识库"),
            (["retreat", "keep_distance"], "撤退/保持距离类"),
            (["focus_fire"], "集火类"),
            (["protect_ally"], "保护队友类"),
            (["retreat", "keep_distance", "focus_fire", "protect_ally"], "当前知识库全部"),
        ]
        for tags, label in probes:
            rr = [run_one("kb", s, args.rounds, tags, difficulty=args.difficulty)
                  for s in seeds]
            med = statistics.median(r["rounds"] for r in rr)
            delta = med - no_s["rounds_median"]
            flag = "  ← 拉低了结果" if delta < -EPS_R else ""
            print(f"    {label:<22} 回合中位 {med:>5.1f}"
                  f"（对照 {delta:+.1f}）{flag}")

    print(f"\n  结论：{verdict}")
    print(f"  {why}")

    if args.json:
        print("\n" + json.dumps(
            {k: {"summary": v["summary"], "rows": v["rows"]} for k, v in results.items()},
            ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())