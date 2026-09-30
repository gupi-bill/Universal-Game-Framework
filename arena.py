#!/usr/bin/env python3
"""
arena.py · 可复现的对局模拟环境（v2.0 / P2 闭环实证的地面设施）
=================================================================

## 为什么需要它

`perception.mock` 只能做「链路能不能跑通」的验证：HP 恒 100、敌人恒 6 个、
坐标按 vx/vy 往返漂移。这种场景下**决策质量无法被度量** —— 无论知识库
是空的还是塞满战术，跑出来的轨迹完全一样。

于是「越打越强」这句话一直无法证伪：M2 里程碑卡在这儿。

本模块把 mock 从「固定画面」升级为「可结算的对局」：

- **seed 驱动**：同一个 seed 得到同一局，不同 seed 得到不同的局
- **有代价**：敌人接触玩家扣血，血空则该局失败
- **有难度曲线**：随回合推进，敌人加速、数量增加
- **可复现**：seed + 动作序列 → 确定的最终状态（给 A/B 统计用）

这样「学过的组」和「没学过的组」才能跑出可比的数据。

## 它不是什么

- 不是游戏模拟器。只实现「移动 → 可能被击中 → 掉血 → 死亡」这一条最简因果链，
  因为只有这条链能让「动作选择」产生可度量的后果。
- 不是 mock 的替代品。`perception.mock` 继续负责「链路通不通」，
  arena 负责「决策好不好」，两者互补。

## 用法

```python
from arena import Arena

a = Arena(seed=42, max_hp=100, contact_radius=28)
for round_no in range(60):
    world = a.step(action="move", x=0.5)     # 动作由决策层给
    if world["over"]:
        break

print(world["hp"], world["over"], world["rounds"])
```

CLI 自检：

```bash
python arena.py --self-test          # 确定性 + 因果链自检
python arena.py --ab --seed 1 --rounds 200   # 跑 A/B 对照
```
"""
from __future__ import annotations

import argparse
import json
import math
import random
from dataclasses import dataclass, field, asdict

# 屏幕尺寸约定与 perception_server.screen_size() 对齐（保守取 1920x1080）
DEFAULT_W = 1920.0
DEFAULT_H = 1080.0


@dataclass
class ArenaConfig:
    """一个场景的全部可调项。"""

    seed: int = 0
    max_hp: int = 100
    #: 玩家与敌人距离小于此值即判定接触
    contact_radius: float = 28.0
    #: 每次接触扣多少血
    contact_damage: int = 12
    #: 敌人初始数量
    enemies: int = 6
    #: 每多少回合多来一个敌人（0 = 不增加）
    spawn_every: int = 8
    #: 敌人基础速度（px/秒）
    enemy_speed: float = 90.0
    #: 每多少回合敌人加速一次，以及加速幅度
    ramp_every: int = 10
    ramp_step: float = 18.0
    #: 玩家最大速度（决定「逃得掉」的上限）
    player_speed: float = 190.0
    #: 每回合模拟多少秒（主循环一圈 ≈ 0.5s，但 arena 用自己的步长）
    dt: float = 0.5
    width: float = DEFAULT_W
    height: float = DEFAULT_H

    # --- v2.0 P2 第二轮新增 ---
    # 原 arena 里 player 只能跑，不能输出、也不能撤离。
    # 这导致「集火」和「撤退」两类知识**在结构上就没有可用空间** ——
    # 防守就是站着挨打，撤退也跑不掉。于是 P2 无法被证实也无法被证伪。
    # 加下面两项，给知识留出可以生效的场景。

    #: 每次 attack 造成多少伤害（0 = 无输出手段）
    attack_damage: int = 0
    #: 射速：每多少回合可攻击一次
    attack_cooldown: int = 1
    #: 玩家血量低于此比例后进入「撤退窗口」，敌人不再追击
    #: （模拟「脱离接触」）。设 0 表示永远无法脱离。
    safe_hp_ratio: float = 0.0
    #: 撤离目标点（归一化）。玩家到达即脱离战斗并结算存活。
    exfil_x: float | None = None
    #: 撤离点半径（归一化，横向）
    exfil_radius: float = 0.06


@dataclass
class ArenaState:
    """一局的状态。只存数字，方便序列化和断言。"""

    hp: int = 100
    max_hp: int = 100
    round_no: int = 0
    over: bool = False
    survived: bool = False
    hits_taken: int = 0
    dodges: int = 0
    player_x: float = 0.0
    player_y: float = 0.0
    enemies: list = field(default_factory=list)
    score: int = 0
    kills: int = 0
    attacks: int = 0
    exfiltrated: bool = False
    _cd: int = 0


class Arena:
    """可复现的最小对局环境。

    确定性来自两处，都必须固定：
      1. `random.Random(seed)` —— 敌人出生点与方向
      2. 每回合的 dt 是常量 —— 不用真实时间
    因此同一 seed + 同一动作序列 = 同一结果。
    """

    def __init__(self, cfg: ArenaConfig | None = None, **kw):
        self.cfg = cfg or ArenaConfig(**kw)
        self.rnd = random.Random(self.cfg.seed)
        self.st = ArenaState(max_hp=self.cfg.max_hp)
        self.st.hp = self.cfg.max_hp
        self._init_player()
        self._spawn(self.cfg.enemies)

    # ---------------------------------------------------------------- 私有

    def _init_player(self) -> None:
        self.st.player_x = self.cfg.width * 0.5
        self.st.player_y = self.cfg.height * 0.86

    def _spawn(self, n: int) -> None:
        """在屏幕上半部随机放 n 个敌人。"""
        for _ in range(max(0, n)):
            self.st.enemies.append({
                "x": self.rnd.uniform(self.cfg.width * 0.08, self.cfg.width * 0.92),
                "y": self.rnd.uniform(self.cfg.height * 0.10, self.cfg.height * 0.45),
                "vx": self.rnd.choice((-1, 1)) * self.cfg.enemy_speed * self.rnd.uniform(0.7, 1.3),
                "vy": 0.0,
                "hp": 10,
            })

    def _nearest_enemy(self):
        """射程内最近的敌人。射程 = 半个屏宽（px）。"""
        if not self.st.enemies:
            return None
        reach = self.cfg.width * 0.5
        best, best_d = None, float("inf")
        for e in self.st.enemies:
            d = math.hypot(e["x"] - self.st.player_x, e["y"] - self.st.player_y)
            if d <= reach and d < best_d:
                best_d, best = d, e
        return best

    def _speed_now(self) -> float:
        """当前回合的敌人速度（含难度爬升）。"""
        steps = self.st.round_no // max(1, self.cfg.ramp_every)
        return self.cfg.enemy_speed + steps * self.cfg.ramp_step

    @staticmethod
    def _clamp(v: float, lo: float, hi: float) -> float:
        return lo if v < lo else (hi if v > hi else v)

    # ---------------------------------------------------------------- 公开

    def step(self, action: str = "idle", x: float | None = None, y: float | None = None) -> dict:
        """推进一个回合。

        action: idle / move / attack / defend / retreat
        x, y: 0~1 的归一化目标位置（move 时用）

        返回本回合结束后的世界快照（结构与 perception 契约对齐，
        这样可以直接喂给 predictor / combat_judge）。
        """
        if self.st.over:
            return self.snapshot()

        c = self.cfg
        self.st.round_no += 1

        # --- 玩家移动 -----------------------------------------------------
        # 动作影响可达范围：attack 不动（输出优先），defend 慢，move 全速。
        mobility = {"move": 1.0, "retreat": 0.85, "idle": 0.35,
                    "attack": 0.15, "defend": 0.5}.get(action, 0.5)
        if x is not None:
            tx = self._clamp(float(x), 0.0, 1.0) * c.width
            ty = self._clamp(float(y) if y is not None else self.st.player_y / c.height, 0.0, 1.0) * c.height
            dx = tx - self.st.player_x
            dy = ty - self.st.player_y
            dist = math.hypot(dx, dy) or 1.0
            budget = c.player_speed * mobility * c.dt
            if dist <= budget:
                self.st.player_x, self.st.player_y = tx, ty
            else:
                self.st.player_x += dx / dist * budget
                self.st.player_y += dy / dist * budget
        self.st.player_x = self._clamp(self.st.player_x, 0.0, c.width)
        self.st.player_y = self._clamp(self.st.player_y, 0.0, c.height)

        # --- 撤离判定（P2 第二轮）：先于敌人推进 -------------------------
        # 「脱离接触」是撤退类知识唯一可能有用的地方。
        # 判据：血量低于 safe_hp_ratio 且已到撤离点附近 → 本局以存活结算。
        if (c.exfil_x is not None and c.safe_hp_ratio > 0
                and (self.st.hp / max(1, self.st.max_hp)) <= c.safe_hp_ratio):
            if abs(self.st.player_x - c.exfil_x * c.width) <= c.exfil_radius * c.width:
                self.st.exfiltrated = True
                self.st.over = True
                self.st.survived = True
                self.st.score += 100
                return self.snapshot()

        # --- 难度爬升 -----------------------------------------------------
        if c.spawn_every and self.st.round_no % max(1, c.spawn_every) == 0:
            self._spawn(1)

        # --- 敌人推进 + 接触判定 -----------------------------------------
        sp = self._speed_now()
        hit = False
        for e in self.st.enemies:
            # 朝玩家缓慢靠拢，带一点横向摆动（三角波近似）
            e["vy"] = math.sin(self.st.round_no * 0.31) * 12.0
            ddx = self.st.player_x - e["x"]
            ddy = self.st.player_y - e["y"]
            d = math.hypot(ddx, ddy) or 1.0
            toward = min(1.0, d / 220.0)          # 越近追得越急
            step = sp * toward * c.dt
            e["x"] = self._clamp(e["x"] + ddx / d * step, 0.0, c.width)
            e["y"] = self._clamp(e["y"] + ddy / d * step, 0.0, c.height)

            if math.hypot(self.st.player_x - e["x"], self.st.player_y - e["y"]) <= c.contact_radius:
                hit = True
                e["x"] = self._clamp(e["x"] - ddx / d * 60.0, 0.0, c.width)
                e["y"] = self._clamp(e["y"] - ddy / d * 60.0, 0.0, c.height)

        if hit:
            self.st.hp -= c.contact_damage
            self.st.hits_taken += 1
        else:
            self.st.dodges += 1

        # --- 远程输出（P2 第二轮）-----------------------------------------
        # 原 arena 里 attack 只有 +3 分的装饰性收益，导致「集火」类知识
        # 无论生效与否都不改变结果 —— 知识有没有用都测不出来。
        # 现在 attack 有真实杀伤力：打到射程内最近的敌人。
        if self.st._cd > 0:
            self.st._cd -= 1
        if action == "attack" and c.attack_damage > 0 and self.st._cd <= 0:
            self.st._cd = max(0, c.attack_cooldown - 1)
            self.st.attacks += 1
            self.st.score += 3
            target = self._nearest_enemy()
            if target is not None:
                target["hp"] = target.get("hp", 10) - c.attack_damage
                if target["hp"] <= 0:
                    self.st.enemies.remove(target)
                    self.st.kills += 1
                    self.st.score += 20

        # --- 结算 ---------------------------------------------------------
        if self.st.hp <= 0:
            self.st.hp = 0
            self.st.over = True
            self.st.survived = False

        return self.snapshot()

    def close(self, survived: bool | None = None) -> ArenaState:
        """主动结束这一局（由主循环的回合上限触发时调用）。"""
        self.st.over = True
        self.st.survived = self.st.hp > 0 if survived is None else bool(survived)
        return self.st

    def snapshot(self) -> dict:
        """结构对齐 perception 契约，可直接喂给 predictor / combat_judge。"""
        hp_ratio = self.st.hp / max(1, self.st.max_hp)
        return {
            "player": {
                "alive": self.st.hp > 0,
                "hp": self.st.hp,
                "max_hp": self.st.max_hp,
                "x": round(self.st.player_x, 1),
                "y": round(self.st.player_y, 1),
                "power_score": round(40 + 60 * hp_ratio, 1),
                "petal_set": "shoot",
                "talent": "none",
            },
            "entities": [
                {
                    "raw_id": f"alien_{i}",
                    "rarity": "alien_drone" if i % 3 else "alien_boss",
                    "x": round(e["x"], 1),
                    "y": round(e["y"], 1),
                    "category": "",
                }
                for i, e in enumerate(self.st.enemies)
            ],
            "teammates": [],
            "afk_popup": False,
            "arena": {
                "seed": self.cfg.seed,
                "round_no": self.st.round_no,
                "hp_ratio": round(hp_ratio, 3),
                "hits": self.st.hits_taken,
                "score": self.st.score,
                "kills": self.st.kills,
                "exfiltrated": self.st.exfiltrated,
            },
        }


# ====================================================================== CLI


def _self_test() -> int:
    """自检：确定性与因果链。"""
    ok = True

    # 1. 同 seed 同动作序列 → 同结果
    acts = [("move", 0.3), ("move", 0.7), ("attack", None), ("move", 0.2)] * 12
    a = Arena(seed=7)
    b = Arena(seed=7)
    for act, x in acts:
        a.step(act, x)
        b.step(act, x)
    same = (a.st.hp, a.st.score, a.st.round_no) == (b.st.hp, b.st.score, b.st.round_no)
    print(f"  {'✓' if same else '✗'} 确定性：同 seed 同动作 → 同结果")
    ok &= same

    # 2. 不同 seed → 不同局面
    # 比敌人生成位置，而不是存活统计：两个 seed 下玩家都可能恰好死在
    # 同一回合、拿到同样的分数，但敌人初始位置一定不同。
    c = Arena(seed=8)
    gen_diff = [e["x"] for e in c.st.enemies] != [e["x"] for e in a.st.enemies]
    for act, x in acts:
        c.step(act, x)
    trail_diff = ([e["x"] for e in c.st.enemies] != [e["x"] for e in a.st.enemies]
                  or c.st.hits_taken != a.st.hits_taken
                  or c.st.score != a.st.score)
    diff = gen_diff and trail_diff
    print(f"  {'✓' if diff else '✗'} 差异性：不同 seed → 不同生成与轨迹")
    ok &= diff

    # 3. 因果链：站着不动会被打（hp 掉），一直跑能活（hp 掉得少）
    still = Arena(seed=3)
    for _ in range(40):
        still.step("idle")
    mover = Arena(seed=3)
    for i in range(40):
        # 朝远离敌人中心的方向跑
        mover.step("move", 0.9 if i % 2 else 0.1)
    better = mover.st.hp > still.st.hp
    print(f"  {'✓' if better else '✗'} 因果链：移动比发呆活得久"
          f"（发呆 hp={still.st.hp} / 移动 hp={mover.st.hp}）")
    ok &= better

    # 4. 死亡能被结算
    dead = Arena(seed=3, contact_damage=50, enemies=4)
    for _ in range(80):
        if dead.st.over:
            break
        dead.step("idle")
    print(f"  {'✓' if dead.st.over else '✗'} 死亡判定：血空即结束"
          f"（hp={dead.st.hp} over={dead.st.over}）")
    ok &= dead.st.over

    # 5. 攻击手段真的有杀伤力（P2 第二轮）
    shooter = Arena(ArenaConfig(seed=3, enemies=4, attack_damage=4,
                                 attack_cooldown=1, ramp_step=0))
    for _ in range(40):
        if shooter.st.over or shooter.st.kills > 0:
            break
        shooter.step("attack")
    killed = shooter.st.kills > 0
    print(f"  {'✓' if killed else '✗'} 攻击有效：attack 能击杀敌人"
          f"（击杀 {shooter.st.kills}）")
    ok &= killed

    # 6. 撤离点有效：低血时到达撤离点即以存活结算
    evader = Arena(ArenaConfig(seed=3, enemies=3, attack_damage=0,
                               safe_hp_ratio=0.9, exfil_x=0.03,
                               exfil_radius=0.2, ramp_step=0,
                               contact_damage=5))
    for _ in range(120):
        if evader.st.over:
            break
        evader.step("move", 0.02)      # 一直往撤离点跑
    escaped = evader.st.exfiltrated and evader.st.survived
    print(f"  {'✓' if escaped else '✗'} 撤离有效：低血时到达撤离点即存活结算"
          f"（hp={evader.st.hp} exfil={evader.st.exfiltrated}）")
    ok &= escaped

    # 7. 攻击与撤离不该破坏确定性
    a2, b2 = Arena(ArenaConfig(seed=5, attack_damage=4)), Arena(ArenaConfig(seed=5, attack_damage=4))
    for i in range(50):
        act = ["move", "attack", "defend"][i % 3]
        x = [0.2, None, 0.9][i % 3]
        a2.step(act, x)
        b2.step(act, x)
    same2 = (a2.st.hp, a2.st.kills, a2.st.score) == (b2.st.hp, b2.st.kills, b2.st.score)
    print(f"  {'✓' if same2 else '✗'} 确定性（含攻击/撤离）：同 seed 同动作 → 同结果")
    ok &= same2

    print("\n" + ("arena 自检通过" if ok else "arena 自检失败"))
    return 0 if ok else 1


def _ab(seed: int, rounds: int) -> int:
    """极简 A/B：只做「有伤害」与「无伤害」两种策略的对照。

    真正的 P2 实证要用 combat_judge + knowledge_loop 驱动决策，
    这里是它的地基自检：证明这个环境确实能区分好坏策略。
    """
    def run(policy):
        a = Arena(seed=seed)
        for i in range(rounds):
            if a.st.over:
                break
            snap = a.snapshot()
            action, x = policy(a, snap, i)
            a.step(action, x)
        a.close()
        return a.st

    def passive(a, snap, i):
        return "idle", None

    def evasive(a, snap, i):
        ex = sum(e["x"] for e in a.st.enemies) / max(1, len(a.st.enemies))
        return "move", 0.05 if ex > a.cfg.width / 2 else 0.95

    p, e = run(passive), run(evasive)
    print(f"  seed={seed} rounds={rounds}")
    print(f"    发呆    hp={p.hp:>4}  被击中={p.hits_taken:>3}  得分={p.score}")
    print(f"    走位    hp={e.hp:>4}  被击中={e.hits_taken:>3}  得分={e.score}")
    verdict = "走位策略更好" if e.hp > p.hp else "无差异或更差"
    print(f"    结论：{verdict}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="arena · 可复现对局模拟环境")
    ap.add_argument("--self-test", action="store_true", help="跑自检")
    ap.add_argument("--ab", action="store_true", help="跑极简 A/B 对照")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--rounds", type=int, default=200)
    args = ap.parse_args(argv)

    if args.self_test:
        return _self_test()
    if args.ab:
        return _ab(args.seed, args.rounds)
    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())