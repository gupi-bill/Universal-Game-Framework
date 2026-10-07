# ===========================================================================
# 14. 主循环
# ===========================================================================
LEARNING_STATS = LearningStats()


def _autopilot_action(state: FramePayload, predictions: list, ev: dict, kb_text: str) -> ActionDict:
    """选动作：优先让 LLM 决策，没密钥就走规则。"""
    return llm_decide(state, predictions, ev, kb_text)


def _write_checkpoint(
    path: str,
    game: str,
    rounds: int,
    deaths: int,
    set_switches: int,
    deaths_cycle: int,
    current_set: str,
):
    """ROADMAP v2 #6：主循环现场落盘，进程被杀后 run --resume 恢复。"""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "schema_version": 1,
                    "game": game,
                    "rounds": rounds,
                    "deaths": deaths,
                    "set_switches": set_switches,
                    "deaths_cycle": deaths_cycle,
                    "current_set": current_set,
                    "ts": datetime.now().isoformat(timespec="seconds"),
                },
                f,
                ensure_ascii=False,
            )
    except OSError:
        pass


def run_agent(
    max_rounds: int = 0,
    interval: float | None = None,
    max_hours: float = 0.0,
    resume: bool = False,
) -> dict:
    """主循环：感知 → 预判 → 评估 → 知识 → 决策 → 动作 → 记忆 → 复盘 → 汇报。"""
    interval = safe_float(interval if interval is not None else cfg_get("agent.loop_interval", 0.5), 0.5)
    game = active_game()
    perception = Perception()
    evaluator = CombatEvaluator()
    dead_threshold = safe_int(cfg_get("agent.death_frame_threshold", 8), 8)
    boss_interval = safe_float(cfg_get("agent.boss_memory_interval", 12), 12)
    learn_interval = safe_int(cfg_get("agent.learning_stats_interval", 24), 24)
    report_every = safe_int(cfg_get("agent.report_every", 0), 0)
    boss_sample_max = safe_int(cfg_get("agent.boss_sample_max", 120), 120)

    log("=" * 58)
    log(f"  Universal-Game-Framework v{VERSION} 单文件 Agent 启动")
    log(f"  {runtime_mode_text()}")
    if dry_run():
        log("  模式: DRY-RUN —— 只记录动作，不碰真实键鼠")
    log("=" * 58)

    log("[清理] " + kb_maintain())
    seeded = seed_knowledge(game)
    if seeded:
        log(f"[知识] 已补种 {len(seeded)} 份 seed 知识到 knowledge_md/{game}/")
    info = resume_info()
    resumable = session_record_start(game)
    if resumable and info:
        log("[续玩] " + info)
        st = session_load()
        st["resumed"] = True
        session_save(st)

    rounds = deaths = death_streak = set_switches = 0
    skipped = 0
    action_counts: dict = {}
    current_set = str(cfg_get("combat.default_set", "combat"))
    state: FramePayload = {}
    boss_obs: list = []
    boss_samples: dict = {}
    learn_buf: list = []
    deaths_cycle = 0
    last_boss_memory = 0.0
    paused = False
    t_start = time.time()
    deadline = t_start + safe_float(max_hours) * 3600 if safe_float(max_hours) > 0 else 0.0
    clean_exit = False
    ckpt_path = os.path.join(RUN_LOGS, "loop_checkpoint.json")
    ckpt_interval = safe_int(cfg_get("agent.checkpoint_interval", 30), 30)
    if resume:
        ck = _read_json(ckpt_path, {})
        if isinstance(ck, dict) and ck:
            if ck.get("game") == game:
                rounds = safe_int(ck.get("rounds"))
                deaths = safe_int(ck.get("deaths"))
                set_switches = safe_int(ck.get("set_switches"))
                deaths_cycle = safe_int(ck.get("deaths_cycle"))
                if ck.get("current_set"):
                    current_set = str(ck.get("current_set"))
                log(
                    f"[续跑] 检查点恢复：回合={rounds} 死亡={deaths} 套装={current_set}（{ck.get('ts', '?')}）"
                )
            else:
                log(f"[续跑] 检查点属于游戏 {ck.get('game')!r}，与当前 {game!r} 不符，从头开始")

    log("[Agent] 进入游戏主循环...\n")
    try:
        while True:
            if max_rounds and rounds >= max_rounds:
                log("[Agent] 达到最大轮数，退出")
                clean_exit = True
                break
            if deadline and time.time() >= deadline:
                log(f"[Agent] 达到时长上限 {max_hours}h，收尾退出")
                clean_exit = True
                break
            rounds += 1

            if reload_if_changed():
                log("[配置] config.yaml / 游戏档案已变更，热加载完成")

            if mouse_in_corner():
                if not paused:
                    log("[安全] 鼠标在屏幕角落，暂停 Agent")
                    paused = True
                time.sleep(1)
                continue
            if paused:
                log("[安全] 鼠标离开角落，恢复 Agent")
                paused = False

            # 1. 感知
            state = perception.perceive()
            if state.get("error"):
                log(f"[感知] 异常: {state['error']}")
                PREDICTOR.reset()
                time.sleep(1)
                continue
            if state.get("afk_popup"):
                log("[AFK] 检测到人机验证弹窗，跳过本回合（可用 action move 手动点）")
                skipped += 1
                time.sleep(interval)
                continue

            _praw = state.get("player")
            player = _praw if isinstance(_praw, dict) else {}

            # 2. 死亡防抖
            if not player.get("alive", True):
                death_streak += 1
                if death_streak >= dead_threshold:
                    deaths += 1
                    deaths_cycle += 1
                    log_event("death", round=rounds, deaths=deaths)
                    if should_review(state):
                        log(review_round(False, "玩家死亡，复盘本局", state))
                    else:
                        log("[复盘] 普通小怪局，不生成复盘（省硬盘）")
                        PREDICTOR.reset()
                    death_streak = 0
                    evaluator.invalidate()
                    time.sleep(2)
                    continue
            else:
                death_streak = 0

            # 3. 预判
            predictions = PREDICTOR.all_entities()

            # 4. 战斗评估
            teammates = state.get("teammates") or []
            ev = evaluator.evaluate(player, predictions, teammates)

            # 5. 换套（去抖）
            rec = ev.get("recommended_set")
            if rec and rec != current_set:
                res = switch_set(rec)
                kb_append(
                    "player_tactics",
                    f"- {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} "
                    f"决策={ev.get('decision')} 心态={ev.get('mindset')} "
                    f"威胁比={ev.get('threat_ratio')} → 换 {rec} 套",
                    game=game,
                )
                log(f"[套装] {current_set} → {rec}（{res}）")
                current_set = rec
                set_switches += 1

            # 6. 检索知识 + 记账
            keyword = "boss" if ev.get("has_highest_boss") else "战术"
            kb_text = kb_search(keyword, game=game)
            hit = kb_is_hit(kb_text)
            learn_buf.append((keyword, hit))
            LEARNING_STATS.record_search(game, hit)

            # 7. 决策
            action = _autopilot_action(state, predictions, ev, kb_text) or {"action": "idle"}
            if action.get("source") == "kb" or (hit and extract_tactics(kb_text)):
                LEARNING_STATS.record_citation(game)

            # 8. 走位钳制 + 拟人抖动
            atype = action.get("action", "idle")
            if atype == "move":
                sx, sy = scale_coords(action.get("x", 400), action.get("y", 300))
                tx, ty = clamp_to_safe_zone(sx, sy)
                tx, ty = apply_jitter(tx, ty)
                out = game_action("move", int(tx), int(ty))
            else:
                out = game_action(atype)
            action_counts[atype] = action_counts.get(atype, 0) + 1
            log_event(
                "decision",
                round=rounds,
                action=atype,
                source=str(action.get("source") or ""),
                decision=ev.get("decision"),
                mindset=ev.get("mindset"),
            )
            if any(k in out for k in ("错误", "必须提供", "未知动作")):
                log(f"[动作] 异常: {out}")

            # 9. BOSS 行为观察
            if ev.get("has_highest_boss") or any(e.get("category") == "boss" for e in predictions):
                px, py = safe_float(player.get("x")), safe_float(player.get("y"))
                for e in predictions:
                    if e.get("category") not in ("boss", "highest_boss"):
                        continue
                    uid = f"{e.get('raw_id', '?')}({e.get('rarity', '?')})"
                    boss_obs.append(
                        f"{datetime.now().strftime('%H:%M:%S')} {uid} "
                        f"位置({e.get('x_now')},{e.get('y_now')}) "
                        f"预判({e.get('x_predict')},{e.get('y_predict')}) "
                        f"决策={ev.get('decision')}"
                    )
                    s = boss_samples.setdefault(uid, [])
                    if len(s) >= boss_sample_max:
                        s.pop(0)
                    s.append((e.get("x_now", 0), e.get("y_now", 0), px, py))

            now = time.time()
            if now - last_boss_memory > boss_interval:
                write_boss_memory(boss_obs, boss_samples)
                boss_obs = []
                last_boss_memory = now

            # 10. 快照 / 进度汇报
            if rounds % 2 == 0:
                write_snapshot(
                    rounds,
                    deaths,
                    player,
                    predictions,
                    ev,
                    game,
                    action_source=str(action.get("source") or ""),
                )
            if ckpt_interval > 0 and rounds % ckpt_interval == 0:
                _write_checkpoint(ckpt_path, game, rounds, deaths, set_switches, deaths_cycle, current_set)
            if report_every and rounds % report_every == 0:
                notify_progress(rounds, deaths)

            # 11. 学习统计 + 自动调参
            if learn_interval and rounds % learn_interval == 0 and learn_buf:
                hits = sum(1 for _k, h in learn_buf if h)
                kb_append(
                    "learning_stats",
                    f"- {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} 汇总：{hits}/{len(learn_buf)} 次命中",
                    game=game,
                )
                log(auto_tune(hits=hits, attempts=len(learn_buf), deaths_extra=deaths_cycle))
                learn_buf = []
                deaths_cycle = 0

            log(
                f"[回合 {rounds}] HP={player.get('hp')} 敌人={len(predictions)} "
                f"队友={len(teammates)} 决策={ev.get('decision')} 套装={current_set} "
                f"心态={ev.get('mindset')} → {atype}"
            )
            time.sleep(interval)

    except KeyboardInterrupt:
        log("\n[Agent] 收到中断信号")
    finally:
        try:
            write_boss_memory(boss_obs, boss_samples)
        except Exception as e:
            log(f"[记忆] 退出前写 BOSS 记忆失败: {e}")
        try:
            log(review_round(True, "Agent 正常退出", state))
        except Exception as e:
            log(f"[复盘] 退出前复盘失败: {e}")
        elapsed = time.time() - t_start
        speed = (rounds / elapsed) if elapsed > 0 else 0.0
        log(
            "[汇总] "
            + " | ".join(
                [
                    f"回合={rounds}",
                    f"死亡={deaths}",
                    f"跳过帧={skipped}",
                    f"换套={set_switches}",
                    "动作=" + (",".join(f"{k}x{v}" for k, v in sorted(action_counts.items())) or "无"),
                    f"耗时={elapsed:.1f}s（{speed:.1f} 回合/秒）",
                ]
            )
        )
        try:
            LEARNING_STATS.save()
            log("[知识] " + LEARNING_STATS.summary(game))
        except Exception as e:
            log(f"[知识] 指标落盘失败: {e}")
        report_text = generate_report()
        try:
            actions = notify(quiet=True)
            session_record_end(game, rounds, deaths, report_text)
            log("[汇报] " + "；".join(actions))
        except Exception as e:
            log(f"[汇报] 收尾失败: {e}")
        if clean_exit:
            with contextlib.suppress(OSError):
                os.remove(ckpt_path)
        elif rounds > 0:
            _write_checkpoint(ckpt_path, game, rounds, deaths, set_switches, deaths_cycle, current_set)
            log(f"[续跑] 非正常收尾，检查点已保存（回合={rounds}），run --resume 可恢复")

    return {
        "rounds": rounds,
        "deaths": deaths,
        "skipped": skipped,
        "set_switches": set_switches,
        "actions": action_counts,
        "elapsed": round(time.time() - t_start, 2),
    }


def runtime_mode_text() -> str:
    m = runtime_mode()
    return (
        f"模式={m['mode']} | 感知={m['perception_backend']} | "
        f"LLM={m['llm']} | VLM={m['vlm']} | 游戏={m['game']}"
    )


