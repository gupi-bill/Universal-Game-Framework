# ===========================================================================
# 15. CLI
# ===========================================================================
GUIDE = """# Universal-Game-Framework · 单文件游戏 Agent 使用手册

## 它是什么
一个能自己跑的游戏 Agent：看画面 → 预判 → 评估 → 出动作 → 查/写经验 → 复盘 → 汇报。
不需要 MCP、不需要别的 Agent 托管，一条命令就能开跑。

## 最常用的三条命令
    python agent.py run --dry-run --rounds 20   # 离线试跑 20 回合（不动键鼠）
    python agent.py run                          # 真机开跑（需感知服务 + pyautogui）
    python agent.py guide                        # 看这份手册

## 全部命令
    run      主循环（--rounds N / --interval S / --hours H / --resume / --game NAME / --dry-run）
    mode     查看运行模式（dry-run / 感知后端 / LLM / VLM / 当前游戏）
    guide    本手册
    perceive 手工取一帧画面状态
    predict  手工取一帧 + 全实体 1.2s 预判
    action   执行一个动作：action move --x 100 --y 200 / action attack
    set      切套装：set retreat
    afk      查看 AFK 弹窗处理指引
    kb       知识库：list / search / write / append / export / import / boss(--top N 排行) /
             tactic / clean / history / rollback / maintain / stats
    learn    视频学习：learn video.mp4 或 learn --url <链接> --frames 5
    report   生成一份对局报告（写 run_logs/，可选 Webhook）
    doctor   环境体检：依赖/目录/档案/感知/密钥，✓⚠✗ 清单（致命项退出码 1）
    config-check  体检 config.yaml / tuned_overrides.yaml（未知键/类型/安全区间）
    bench    性能基准：--rounds N，分段计时 感知/预判/评估/决策/动作
    panel    本地监控面板（纯标准库，只读快照/事件/日志，Ctrl+C 停止）
    logs     查看运行日志：--tail N / --grep KW / --events --kind decision / --stats
    session  看会话记忆与历史战绩（--all 全部游戏汇总）
    brief    开局侦察报告：档案/知识库/战绩/调参一屏聚合
    replay   对局回放：事件时间线（--tail N 只看最后 N 条）
    tune     自动调参：tune --status / tune --reset
    selftest 离线自检：不碰键鼠、不用密钥，跑通全链路并断言关键产物

## 关键开关（环境变量）
    UGF_DRY_RUN=1              不执行真实键鼠（推荐先这样试跑）
    UGF_PERCEPTION_BACKEND=... mock（离线合成）/ http（外部检测服务）/ local（ONNX 本地推理）/ template（模板匹配）
    UGF_PERCEPTION_URL=...     外部感知服务地址（留空则用 127.0.0.1:<端口>/perceive）
    AGENT_GAME=space_invaders  切换游戏档案（读 game_profiles/<名字>.yaml）
    LLM_API_URL / LLM_API_KEY / LLM_MODEL     LLM 决策
    VLM_API_URL / VLM_API_KEY / VLM_MODEL     视频学习的视觉模型
    UGF_WEBHOOK_URL=...        报告推送地址

## 注意事项
- 感知：离线用 mock；真机请让外部检测服务返回同样的 payload
  （{"player":{...},"entities":[{raw_id,rarity,x,y}],"teammates":[...],"afk_popup":false}）。
- 预判需要 ≥3 帧历史才有效；confidence < 0.65 时 prediction_trusted=false，别信预判坐标。
- 换局 / 重生后先 `kb clean --target predict`，避免用旧轨迹误判。
- 合规：仅用于本地 / 自建 / 已授权环境，不提供任何绕过他人服务条款的手段。
"""


def _print(obj):
    if isinstance(obj, (dict, list)):
        print(json.dumps(obj, ensure_ascii=False, indent=2))
    else:
        print(obj)


def _cli_kb(args) -> int:
    sub = args.kb_cmd
    # 全局 --game 已由 main() 导出到 AGENT_GAME，这里直接取激活游戏即可
    game = active_game()
    if sub == "list":
        _print(kb_list(game) if not args.all else kb_list())
    elif sub == "search":
        print(kb_search(args.keyword, game if not args.all else ""))
    elif sub == "write":
        content = args.content or (sys.stdin.read() if not sys.stdin.isatty() else "")
        print(kb_write(args.filename, content, game))
    elif sub == "append":
        content = args.content or (sys.stdin.read() if not sys.stdin.isatty() else "")
        print(kb_append(args.filename, content, game))
    elif sub == "export":
        print(kb_export())
    elif sub == "import":
        print(kb_import(args.backup))
    elif sub == "boss":
        if safe_int(getattr(args, "top", 0)) > 0:
            print(kb_boss_ranking(args.top, game))
        else:
            print(kb_query_boss(args.name or ""))
    elif sub == "stats":
        print(kb_stats(game, args.all))
    elif sub == "tactic":
        print(kb_switch_tactic(args.filename))
    elif sub == "clean":
        print(kb_clean(args.target))
    elif sub == "history":
        print(kb_history(args.filename, game))
    elif sub == "rollback":
        print(kb_rollback(args.filename, args.rev, game))
    elif sub == "maintain":
        print(kb_maintain())
    else:
        print(f"未知 kb 子命令: {sub}")
        return 2
    return 0


def selftest(max_rounds: int = 12) -> int:
    """离线自检：全链路跑通 + 断言关键产物。返回 0 表示全过。"""
    os.environ["UGF_DRY_RUN"] = "1"
    os.environ["UGF_PERCEPTION_BACKEND"] = "mock"
    reload_config()
    checks = []

    def check(name, cond):
        checks.append((name, bool(cond)))
        log(("  ✓ " if cond else "  ✗ ") + name)

    log("== 自检 1/6 感知 + 预判 ==")
    p = Perception()
    frames = 0
    for _ in range(5):
        st = p.perceive()
        frames += 1 if "error" not in st else 0
    preds = PREDICTOR.all_entities()
    check("mock 感知可出帧", frames == 5)
    check("预判可输出实体", len(preds) > 0)
    check(
        "预判字段完整",
        all(
            k in preds[0] for k in ("raw_id", "category", "threat_score", "confidence", "prediction_trusted")
        ),
    )

    log("== 自检 2/6 战斗评估 ==")
    player = p._mock.get("player") or {}
    ev = judge_combat(player, preds, [])
    check("评估含 decision", ev.get("decision") in ("fight", "cautious_fight", "retreat"))
    check("评估含 recommended_set", bool(ev.get("recommended_set")))

    log("== 自检 3/6 知识库闭环 ==")
    seed_knowledge(force=True)
    txt = kb_search("战术", active_game())
    check("seed 知识可检索", kb_is_hit(txt))
    check("命中战术带适用条件", len(extract_tactics(txt)) > 0)
    act = decide_action("cautious_fight", extract_tactics(txt), hp_ratio=0.2, threat_ratio_=1.5)
    check("知识可影响决策", act in ("defend", "attack", ""))

    log("== 自检 4/6 复盘 + BOSS 记忆 ==")
    state: FramePayload = {
        "player": {"petal_set": "combat"},
        "entities": [{"raw_id": "mantis", "rarity": "Super"}],
        "teammates": [],
    }
    check("复盘判定命中(BOSS 局)", should_review(state))
    r = review_round(False, "自检复盘", state)
    check("复盘已写入知识库", "已写入知识库" in r)
    o = analyze_boss_behavior([(100, 100, 200, 200), (110, 105, 200, 200), (130, 120, 200, 200)])
    check("BOSS 行为可归纳", "平均距离" in o)
    w = write_boss_memory(["自检：观察到 BOSS 直线推进"], {"mantis(Super)": [(100, 100, 200, 200)] * 4})
    check("BOSS 记忆已写库", "已追加" in w)

    log("== 自检 5/6 视频学习(离线合成帧) ==")
    res = learn_from_video(None, frame_count=5)
    check("视频学习产出文件", os.path.exists(res["file"]))

    log("== 自检 6/6 主循环 + 汇报 + 会话 + 调参 ==")
    out = run_agent(max_rounds=max_rounds, interval=0.0)
    check("主循环跑满回合", out["rounds"] == max_rounds)
    check("动作已执行", sum(out["actions"].values()) == max_rounds)
    rep = generate_report()
    check("报告可生成", "对局报告" in rep)
    check("会话已记账", safe_int(session_load().get("sessions")) >= 1)
    t = auto_tune(hits=1, attempts=10, deaths_extra=1)
    check("自动调参可执行", "调参" in t)
    auto_tuner_reset()
    check("kb 导出可用", "已导出知识库" in kb_export())

    passed = sum(1 for _n, ok in checks if ok)
    log("=" * 58)
    log(f"自检结果：{passed}/{len(checks)} 通过")
    for n, ok in checks:
        if not ok:
            log(f"  未通过：{n}")
    log("=" * 58)
    return 0 if passed == len(checks) else 1


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="agent.py",
        description="Universal-Game-Framework · 单文件游戏 Agent",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例：\n  python agent.py run --dry-run --rounds 20\n"
        "  python agent.py selftest\n  python agent.py guide",
    )
    # dest 与子命令位置参数 game 区分开：--game 是全局切换，位置参数只作用于该子命令
    ap.add_argument(
        "--game",
        default="",
        dest="game_opt",
        metavar="NAME",
        help="指定游戏名（读 game_profiles/<名字>.yaml）",
    )
    ap.add_argument("--version", action="store_true", help="打印版本号并退出")
    sub = ap.add_subparsers(dest="cmd")

    sp = sub.add_parser("run", help="主循环")
    sp.add_argument("--rounds", type=int, default=0, help="最多跑多少回合（0=不限）")
    sp.add_argument("--interval", type=float, default=None, help="每回合间隔秒（默认取配置）")
    sp.add_argument("--dry-run", action="store_true", help="只记录动作，不碰真实键鼠")
    sp.add_argument("--hours", type=float, default=0.0, help="时长上限小时（0=不限，ROADMAP v2 #6）")
    sp.add_argument("--resume", action="store_true", help="从循环检查点续跑（ROADMAP v2 #6）")

    sub.add_parser("mode", help="查看运行模式")
    sub.add_parser("guide", help="使用手册")
    sub.add_parser("perceive", help="手工取一帧画面状态")
    sub.add_parser("predict", help="手工取一帧 + 全实体预判")
    sp = sub.add_parser("session", help="会话记忆与历史战绩")
    sp.add_argument("--all", action="store_true", help="全部游戏汇总（ROADMAP v2 #5）")
    sub.add_parser("afk", help="AFK 弹窗处理指引")

    sp = sub.add_parser("action", help="执行一个动作")
    sp.add_argument("type", choices=list(VALID_ACTIONS))
    sp.add_argument("--x", type=int, default=None)
    sp.add_argument("--y", type=int, default=None)

    sp = sub.add_parser("set", help="切换套装")
    sp.add_argument("name")

    sp = sub.add_parser("kb", help="知识库操作")
    kbsub = sp.add_subparsers(dest="kb_cmd")
    kbsub.add_parser("list")
    kbsub.add_parser("export")
    kbsub.add_parser("maintain")
    k = kbsub.add_parser("search")
    k.add_argument("keyword")
    k.add_argument("--all", action="store_true")
    k = kbsub.add_parser("write")
    k.add_argument("filename")
    k.add_argument("--content", default="")
    k = kbsub.add_parser("append")
    k.add_argument("filename")
    k.add_argument("--content", default="")
    k = kbsub.add_parser("import")
    k.add_argument("backup")
    k = kbsub.add_parser("boss")
    k.add_argument("name", nargs="?", default="")
    k.add_argument("--top", type=int, default=0, help="危险度排行 Top N（ROADMAP v2 #9）")
    k = kbsub.add_parser("tactic")
    k.add_argument("filename")
    k = kbsub.add_parser("clean")
    k.add_argument("--target", default="all", choices=["all", "predict", "frames"])
    k = kbsub.add_parser("history")
    k.add_argument("filename")
    k = kbsub.add_parser("rollback")
    k.add_argument("filename")
    k.add_argument("--rev", type=int, default=1)
    k = kbsub.add_parser("stats")
    k.add_argument("--all", action="store_true", help="全部游戏分区")
    for p in (kbsub.choices["list"], kbsub.choices["export"], kbsub.choices["maintain"]):
        p.add_argument("--all", action="store_true")

    sp = sub.add_parser("learn", help="视频学习")
    sp.add_argument("video", nargs="?", default=None, help="本地视频路径")
    sp.add_argument("--url", default=None, help="在线视频链接（需装 yt-dlp）")
    sp.add_argument("--frames", type=int, default=5, help="离线合成帧数")
    sp.add_argument("--skip", type=int, default=25, help="每 N 帧抽一张")

    sub.add_parser("report", help="生成对局报告")

    sp = sub.add_parser("tune", help="自动调参")
    sp.add_argument("--status", action="store_true")
    sp.add_argument("--reset", action="store_true")

    sp = sub.add_parser("selftest", help="离线自检")
    sp.add_argument("--rounds", type=int, default=12)

    sp = sub.add_parser("profile-check", help="校验游戏档案（按 _template.yaml 规范）")
    sp.add_argument("game", nargs="?", default="", help="档案名（缺省=当前激活游戏）")
    sp.add_argument("--all", action="store_true", help="校验 game_profiles/ 下全部档案")
    sp.add_argument("--strict", action="store_true", help="严格模式：WARN 也判失败")

    sp = sub.add_parser("panel", help="启动本地监控面板（只读快照与日志）")
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--port", type=int, default=None, help="默认取 server.panel_port(5002)")

    sp = sub.add_parser("logs", help="查看运行日志 / 结构化事件流")
    sp.add_argument("--tail", type=int, default=20, help="最后 N 行（默认 20）")
    sp.add_argument("--grep", default="", help="按关键词过滤")
    sp.add_argument("--events", action="store_true", help="看结构化事件流（JSONL）")
    sp.add_argument("--kind", default="", help="事件类型过滤：decision/death/tune/learn/session_end")
    sp.add_argument("--stats", action="store_true", help="事件流聚合统计（ROADMAP v2 #3）")

    sp = sub.add_parser("doctor", help="环境体检（依赖/目录/档案/感知/密钥）")
    sp.add_argument("game", nargs="?", default="", help="体检指定游戏档案（缺省=当前）")

    sp = sub.add_parser("bench", help="分段耗时基准（mock+dry-run，规则决策）")
    sp.add_argument("--rounds", type=int, default=200, help="基准回合数（默认 200）")

    sp = sub.add_parser("brief", help="开局侦察报告（档案/知识库/战绩/调参一屏）")
    sp.add_argument("game", nargs="?", default="", help="指定游戏（缺省=当前）")

    sp = sub.add_parser("replay", help="对局回放：事件时间线（ROADMAP v2 #12）")
    sp.add_argument("--tail", type=int, default=0, help="只看最后 N 条事件")

    sub.add_parser("config-check", help="体检 config.yaml / tuned_overrides.yaml（ROADMAP v2 #14）")
    return ap


def main(argv=None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    if args.version:
        print(f"Universal-Game-Framework {VERSION}")
        return 0
    if args.game_opt:
        os.environ["AGENT_GAME"] = args.game_opt
        reload_config()

    cmd = args.cmd
    if cmd == "guide":
        print(GUIDE)
    elif cmd == "mode":
        _print(runtime_mode())
    elif cmd == "run":
        if getattr(args, "dry_run", False):
            os.environ["UGF_DRY_RUN"] = "1"
            reload_config()
        run_agent(
            max_rounds=args.rounds,
            interval=args.interval,
            max_hours=args.hours,
            resume=args.resume,
        )
    elif cmd == "perceive":
        _print(Perception().perceive())
    elif cmd == "predict":
        st = Perception().perceive()
        for _ in range(2):
            st = Perception().perceive()
        _print({"player": st.get("player"), "predictions": PREDICTOR.all_entities()})
    elif cmd == "action":
        print(game_action(args.type, args.x, args.y))
    elif cmd == "set":
        print(switch_set(args.name))
    elif cmd == "afk":
        print(handle_afk())
    elif cmd == "kb":
        if not args.kb_cmd:
            print(GUIDE)
            return 0
        return _cli_kb(args)
    elif cmd == "learn":
        path = args.video
        if args.url:
            path = download_video(args.url)
            log(f"[学习] 下载结果: {path}")
        r = learn_from_video(path, frame_count=args.frames, skip=args.skip)
        _print(r)
    elif cmd == "report":
        notify()
    elif cmd == "session":
        print(session_summary(all_games=args.all))
    elif cmd == "brief":
        return brief(args.game)
    elif cmd == "replay":
        print(replay(args.tail))
    elif cmd == "config-check":
        return config_check()
    elif cmd == "tune":
        if args.reset:
            print(auto_tuner_reset())
        else:
            print(auto_tuner_status())
    elif cmd == "selftest":
        return selftest(args.rounds)
    elif cmd == "profile-check":
        return profile_check(args.game, check_all=args.all, strict=args.strict)
    elif cmd == "panel":
        run_panel(args.host, args.port)
    elif cmd == "doctor":
        return doctor(args.game)
    elif cmd == "bench":
        return bench(args.rounds)
    elif cmd == "logs":
        if args.stats:
            print(event_stats(args.kind))
        elif args.events:
            for e in read_events(args.tail, args.kind):
                print(json.dumps(e, ensure_ascii=False))
        else:
            p = os.path.join(RUN_LOGS, f"agent_{datetime.now().strftime('%Y%m%d')}.log")
            lines = []
            if os.path.exists(p):
                try:
                    with open(p, encoding="utf-8") as f:
                        lines = f.read().splitlines()
                except OSError:
                    lines = []
            if args.grep:
                lines = [ln for ln in lines if args.grep.lower() in ln.lower()]
            for ln in lines[-max(1, args.tail) :]:
                print(ln)
    else:
        ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
