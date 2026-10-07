#!/usr/bin/env python3
"""
FlorrVLM-Agent 40 分钟长跑仿真器
=================================
不依赖真实游戏画面 / LLM / pyautogui，直接用本地函数模拟完整决策循环：

  每 0.5 秒一轮：
    1. 假世界生成实体（highest_boss / boss / elite / normal 随机游走）
    2. player 位置根据上一轮决策移动 + 抖动
    3. predictor.update_frame_entities → predict_all_entities
    4. combat_judge.CombatEvaluator.evaluate → 决策
    5. 偶尔模拟死亡 → 走 review 写 md
    6. 每 12 秒写 BOSS 观察到 knowledge_md
    7. 记录内存/统计

跑满 DURATION_SEC 秒后打印总结。
"""
import json
import os
import random
import resource
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import predictor
import combat_judge

# 长跑参数
DURATION_SEC = int(os.getenv("BENCH_SEC", "2400"))   # 默认 40 分钟
LOOP_INTERVAL = 0.05   # 仿真每轮间隔（真实游戏 0.5s，仿真加速到 0.05s 但按"游戏时间"记账）
GAME_TICK_PER_ROUND = 0.5  # 每轮 = 0.5 游戏秒
BOSS_LOG_INTERVAL = 12
SCREEN_W, SCREEN_H = 1920, 1080

KB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "knowledge_md")
os.makedirs(KB_DIR, exist_ok=True)


# ---------------------------------------------------------------------------
# 假世界
# ---------------------------------------------------------------------------
class FakeWorld:
    def __init__(self):
        self.player_x, self.player_y = 960.0, 540.0
        self.player_hp = 100.0
        self.player_power = 120.0
        self.alive = True
        self.tick = 0
        self.entities = []
        self.world_start = time.time()
        self._spawn_initial()

    def _spawn_initial(self):
        rarities = [
            ("wasp", "Unique", "highest_boss"),
            ("mole", "Super", "boss"),
            ("rock", "Ultra", "elite"),
            ("rock", "Mythic", "elite"),
            ("bee", "Unusual", "normal"),
            ("ant", "Common", "normal"),
            ("ant", "Common", "normal"),
        ]
        for raw_id, rarity, cat in rarities:
            self.entities.append({
                "raw_id": raw_id,
                "rarity": rarity,
                "x": random.uniform(200, 1700),
                "y": random.uniform(200, 900),
                "vx": random.uniform(-120, 120),
                "vy": random.uniform(-120, 120),
            })

    def step(self):
        """推进一步游戏时间，怪物随机游走+边界反弹，玩家朝威胁方向反应。"""
        self.tick += 1
        # 玩家随时间成长（模拟打怪升级）：40 分钟从 120 涨到 1600
        elapsed_now = time.time() - self.world_start
        self.player_power = 120.0 + (elapsed_now / DURATION_SEC) * 1480.0
        # highest_boss 每 30 秒消失 10 秒（走了），让玩家有 fight 小怪的窗口
        hb = [e for e in self.entities if e.get("rarity") == "Unique"]
        if hb:
            hb[0]["away"] = (elapsed_now % 40) < 10
        for e in self.entities:
            e["x"] += e["vx"] * GAME_TICK_PER_ROUND
            e["y"] += e["vy"] * GAME_TICK_PER_ROUND
            # 边界反弹
            if e["x"] < 50 or e["x"] > SCREEN_W - 50:
                e["vx"] *= -1
            if e["y"] < 50 or e["y"] > SCREEN_H - 50:
                e["vy"] *= -1
            # 偶尔改方向
            if random.random() < 0.02:
                e["vx"] += random.uniform(-80, 80)
                e["vy"] += random.uniform(-80, 80)

    def player_state(self):
        return {
            "x": self.player_x,
            "y": self.player_y,
            "hp": self.player_hp,
            "max_hp": 100.0,
            "power_score": self.player_power,
            "petal_set": "combat",
            "talent": "none",
            "alive": self.alive,
        }

    def move_player_toward(self, tx, ty):
        """玩家朝目标移动一点（模拟决策执行）。"""
        dx = tx - self.player_x
        dy = ty - self.player_y
        dist = (dx ** 2 + dy ** 2) ** 0.5
        if dist > 5:
            step = min(dist, 120 * GAME_TICK_PER_ROUND)
            self.player_x += dx / dist * step
            self.player_y += dy / dist * step

    def take_damage(self, amount):
        self.player_hp = max(0, self.player_hp - amount)
        if self.player_hp <= 0:
            self.alive = False

    def respawn(self):
        self.player_hp = 100.0
        self.alive = True
        self.player_x, self.player_y = 960.0, 540.0
        predictor.reset()


# ---------------------------------------------------------------------------
# 主仿真
# ---------------------------------------------------------------------------
def run():
    start = time.time()
    world = FakeWorld()
    evaluator = combat_judge.CombatEvaluator(debounce_interval=0.7)

    # 统计
    stats = {
        "rounds": 0,
        "deaths": 0,
        "decisions": {"fight": 0, "cautious_fight": 0, "retreat": 0},
        "kb_writes": 0,
        "errors": 0,
        "max_rss_mb": 0,
        "boss_logs_written": 0,
    }
    last_boss_log = 0.0
    death_streak = 0

    print(f"[BENCH] 启动仿真，目标 {DURATION_SEC} 秒（约 {DURATION_SEC/60:.0f} 分钟）")
    print(f"[BENCH] 开始时间 {datetime.now().strftime('%H:%M:%S')}")

    while True:
        elapsed = time.time() - start
        if elapsed >= DURATION_SEC:
            break

        stats["rounds"] += 1
        world.step()

        # 喂给 predictor（away 的实体不算在场上）
        ents_for_pred = [
            {"raw_id": e["raw_id"], "rarity": e["rarity"], "x": e["x"], "y": e["y"]}
            for e in world.entities if not e.get("away")
        ]
        try:
            predictor.update_frame_entities(ents_for_pred)
            preds = predictor.predict_all_entities()
        except Exception as ex:
            stats["errors"] += 1
            preds = []
            if stats["errors"] <= 3:
                print(f"[ERROR] predict: {ex}")

        # 战斗评估
        pstate = world.player_state()
        ctx = combat_judge.CombatContext(
            player=combat_judge.PlayerState(
                hp=pstate["hp"], max_hp=100.0,
                power_score=pstate["power_score"],
                x=pstate["x"], y=pstate["y"],
            ),
            enemies=preds,
            teammates=[],
        )
        try:
            ev = evaluator.evaluate(ctx)
        except Exception as ex:
            stats["errors"] += 1
            ev = {"decision": "fight"}

        dec = ev.get("decision", "fight")
        stats["decisions"][dec] = stats["decisions"].get(dec, 0) + 1

        # 玩家移动
        top = preds[0] if preds else None
        if dec == "retreat" and top:
            rt = combat_judge.calc_retreat_position(ctx, top)
            tx, ty = rt["x"], rt["y"]
        elif dec == "fight" and top:
            tx, ty = top.get("x_now", world.player_x), top.get("y_now", world.player_y)
        else:
            tx, ty = world.player_x + random.uniform(-100, 100), world.player_y + random.uniform(-100, 100)
        tx, ty = combat_judge.apply_jitter(tx, ty)
        world.move_player_toward(tx, ty)

        # 伤害模拟：retreat 时少受伤，fight 时多受伤
        if top and top.get("category") in ("highest_boss", "boss"):
            if dec == "fight":
                world.take_damage(random.uniform(1, 5))
            elif dec == "retreat":
                world.take_damage(random.uniform(0.2, 1))
            else:
                world.take_damage(random.uniform(0.5, 2))

        # 死亡判定（连续 2 帧）
        if not pstate["alive"]:
            death_streak += 1
            if death_streak >= 2:
                stats["deaths"] += 1
                # 写复盘 md
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                try:
                    with open(os.path.join(KB_DIR, f"review_{ts}.md"), "w", encoding="utf-8") as f:
                        f.write(f"# 复盘 {ts}\n\n- 结果: 死亡\n- 面对: {len(world.entities)} 个实体\n- 轮次: {stats['rounds']}\n")
                    stats["kb_writes"] += 1
                except OSError:
                    pass
                world.respawn()
                evaluator.invalidate()
                death_streak = 0
        else:
            death_streak = 0

        # BOSS 观察批量写
        now = time.time()
        if now - last_boss_log >= BOSS_LOG_INTERVAL:
            try:
                with open(os.path.join(KB_DIR, "boss_behavior_log.md"), "a", encoding="utf-8") as f:
                    f.write(f"- {datetime.now().strftime('%H:%M:%S')} 轮次{stats['rounds']} 决策={dec} 敌数={len(preds)}\n")
                stats["boss_logs_written"] += 1
            except OSError:
                pass
            last_boss_log = now

        # 内存采样
        rss_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
        if rss_mb > stats["max_rss_mb"]:
            stats["max_rss_mb"] = rss_mb

        # 每 5 分钟打印一次进度
        if stats["rounds"] % 600 == 0:
            print(f"[进度] {elapsed/60:.1f}min / {DURATION_SEC/60:.0f}min | "
                  f"轮次={stats['rounds']} 死亡={stats['deaths']} "
                  f"决策分布={stats['decisions']} 峰值内存={rss_mb:.0f}MB 错误={stats['errors']}",
                  flush=True)

        time.sleep(LOOP_INTERVAL)

    # 总结
    print("\n" + "=" * 60)
    print(f"[BENCH] 仿真结束，总耗时 {time.time()-start:.1f}s")
    print(f"[BENCH] 总轮次: {stats['rounds']}")
    print(f"[BENCH] 死亡次数: {stats['deaths']}")
    print(f"[BENCH] 决策分布: {stats['decisions']}")
    print(f"[BENCH] KB 复盘写入: {stats['kb_writes']}")
    print(f"[BENCH] BOSS 观察写入: {stats['boss_logs_written']}")
    print(f"[BENCH] 异常次数: {stats['errors']}")
    print(f"[BENCH] 峰值内存: {stats['max_rss_mb']:.1f} MB")
    print("=" * 60)


if __name__ == "__main__":
    run()
