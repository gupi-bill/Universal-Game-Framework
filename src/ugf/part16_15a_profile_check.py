# ===========================================================================
# 15a. 游戏档案校验（profile-check）
# ===========================================================================
SEMANTIC_SETS = ("combat", "tank", "retreat", "chase", "team")
VALID_CHASE_CATEGORIES = ("highest_boss", "boss", "elite", "normal", "player_enemy")
THREAT_KEYS = ("highest_boss", "boss", "elite", "normal", "player_enemy", "player_ally", "unknown")


def _pc_issue(issues: list, level: str, path: str, msg: str):
    issues.append((level, path, msg))


def profile_check_one(game: str) -> list:
    """校验单个游戏档案，返回 [(level, path, msg)]，level ∈ ERROR/WARN。

    规则来源：game_profiles/_template.yaml 的字段注释（安全区间 / 缺失兜底级别）。
    """
    issues: list = []
    g = safe_name(game)
    if not g:
        return [("ERROR", "game", "游戏名为空")]
    path = os.path.join(PROFILE_DIR, f"{g}.yaml")
    if not os.path.exists(path):
        return [("ERROR", "file", f"档案不存在: game_profiles/{g}.yaml")]
    prof = _read_yaml(path)
    if not prof:
        return [("ERROR", "file", f"档案无法解析为 YAML 字典: {g}.yaml（或缺少 pyyaml）")]

    # 一、游戏元信息
    name = str((prof.get("game") or {}).get("name") or "").strip()
    if not name:
        _pc_issue(issues, "ERROR", "game.name", "缺失（必填）")
    elif not re.fullmatch(r"[\w.-]+", name):
        _pc_issue(issues, "ERROR", "game.name", f"含非法字符（只允许 [\\w.-]）: {name!r}")
    elif name != g:
        _pc_issue(issues, "ERROR", "game.name", f"必须等于文件名 {g!r}，当前为 {name!r}")
    if not str((prof.get("game") or {}).get("description") or "").strip():
        _pc_issue(issues, "WARN", "game.description", "缺失（大盘与 brief 显示为空，不阻断）")

    # 二、感知端口
    port = (prof.get("server") or {}).get("perception_port")
    if port is None:
        _pc_issue(issues, "WARN", "server.perception_port", "缺失，将回落 config.yaml 默认端口")
    elif not isinstance(port, int) or isinstance(port, bool) or not 1024 <= int(port) <= 65535:
        _pc_issue(issues, "ERROR", "server.perception_port", f"必须为 1024~65535 的整数，当前: {port!r}")

    # 三、稀有度档位与威胁金字塔
    pred = prof.get("predictor") or {}
    tiers: dict = {}
    for key in ("rarity_highest_boss", "rarity_boss", "rarity_elite", "rarity_normal"):
        vals = pred.get(key)
        if isinstance(vals, list) and vals and all(isinstance(v, str) and v.strip() for v in vals):
            tiers[key] = [v.strip().capitalize() for v in vals]
        else:
            _pc_issue(issues, "ERROR", f"predictor.{key}", "必须为非空字符串列表")
            tiers[key] = []
    seen_pairs: dict = {}
    for key, vals in tiers.items():
        for v in vals:
            if v in seen_pairs:
                _pc_issue(
                    issues,
                    "ERROR",
                    f"predictor.{key}",
                    f"稀有度 {v!r} 同时出现在 {seen_pairs[v]} 与 {key}，档位不允许重叠",
                )
            seen_pairs[v] = key

    threat = pred.get("threat")
    if not isinstance(threat, dict):
        _pc_issue(issues, "ERROR", "predictor.threat", "缺失或非字典（7 个键必须齐全）")
        threat = {}
    for k in THREAT_KEYS:
        v = threat.get(k)
        if v is None:
            _pc_issue(issues, "ERROR", f"predictor.threat.{k}", "缺失（必填）")
        elif not isinstance(v, (int, float)) or isinstance(v, bool) or v < 0:
            _pc_issue(issues, "ERROR", f"predictor.threat.{k}", f"必须为 >=0 的数字，当前: {v!r}")
    nums: dict = {}
    for k in THREAT_KEYS:
        v = threat.get(k)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            nums[k] = float(v)
    chain = [k for k in ("highest_boss", "boss", "elite", "normal") if k in nums]
    for a, b in zip(chain, chain[1:], strict=False):
        if nums[a] < nums[b]:
            _pc_issue(
                issues,
                "ERROR",
                "predictor.threat",
                f"单调性倒置：{a}({nums[a]}) < {b}({nums[b]})，要求逐级递减",
            )
    if isinstance(threat.get("player_ally"), (int, float)) and threat["player_ally"] != 0:
        _pc_issue(issues, "WARN", "predictor.threat.player_ally", "建议固定为 0（非 0 会干扰威胁求和）")

    # 四、战斗配置
    combat = prof.get("combat") or {}
    sets_raw = combat.get("sets")
    if isinstance(sets_raw, list) and sets_raw and all(isinstance(x, str) and x.strip() for x in sets_raw):
        sets = [str(x).strip().lower() for x in sets_raw]
    else:
        _pc_issue(issues, "ERROR", "combat.sets", "必须为非空字符串列表（游戏内真实套装名）")
        sets = []
    default_set = str(combat.get("default_set") or "").strip().lower()
    if not default_set:
        _pc_issue(issues, "ERROR", "combat.default_set", "缺失（必填，运行时换套会落到未知套装）")
    elif sets and default_set not in sets:
        _pc_issue(issues, "ERROR", "combat.default_set", f"必须 ∈ combat.sets {sets}，当前: {default_set!r}")
    cmc = str(combat.get("chase_min_category") or "").strip()
    if not cmc:
        _pc_issue(issues, "ERROR", "combat.chase_min_category", "缺失（必填）")
    elif cmc not in VALID_CHASE_CATEGORIES:
        _pc_issue(
            issues,
            "ERROR",
            "combat.chase_min_category",
            f"非法值 {cmc!r}，可选 {'/'.join(VALID_CHASE_CATEGORIES)}",
        )
    if sets and not (set(sets) & set(SEMANTIC_SETS)) and not isinstance(combat.get("set_map"), dict):
        _pc_issue(
            issues,
            "WARN",
            "combat.set_map",
            "套装名与决策语义(combat/tank/retreat/chase/team)完全不同名却未声明 set_map，"
            "switch_set 会落到「未知套装」",
        )
    tactics = combat.get("tactics")
    if not isinstance(tactics, list) or not [t for t in tactics if str(t or "").strip()]:
        _pc_issue(issues, "WARN", "combat.tactics", "缺失或为空（知识库将没有经验可引用）")

    # 五、mock 感知数据（可选但强烈建议）
    mock = (prof.get("perception") or {}).get("mock")
    if not isinstance(mock, dict):
        _pc_issue(issues, "WARN", "perception.mock", "缺失：离线 dry-run 将退化到内置默认场景")
    else:
        all_rarities = {v for vals in tiers.values() for v in vals}
        player = mock.get("player") or {}
        max_hp = safe_float(player.get("max_hp"), 0)
        if max_hp <= 0:
            _pc_issue(issues, "ERROR", "perception.mock.player.max_hp", "必须 > 0")
        hp = safe_float(player.get("hp"), -1)
        if hp < 0 or (max_hp > 0 and hp > max_hp):
            _pc_issue(issues, "ERROR", "perception.mock.player.hp", f"必须在 0~max_hp 之间，当前: {hp!r}")
        ps = str(player.get("petal_set") or "").strip().lower()
        if sets and ps and ps not in sets:
            _pc_issue(
                issues,
                "ERROR",
                "perception.mock.player.petal_set",
                f"必须 ∈ combat.sets {sets}，当前: {ps!r}",
            )
        for i, ent in enumerate(mock.get("entities") or []):
            if not isinstance(ent, dict):
                _pc_issue(issues, "ERROR", f"perception.mock.entities[{i}]", "必须为字典")
                continue
            for field in ("raw_id", "x", "y"):
                if ent.get(field) is None:
                    _pc_issue(issues, "ERROR", f"perception.mock.entities[{i}].{field}", "缺失（必填）")
            rar = str(ent.get("rarity") or "").strip().capitalize()
            if all_rarities and rar and rar not in all_rarities:
                _pc_issue(
                    issues,
                    "ERROR",
                    f"perception.mock.entities[{i}].rarity",
                    f"{rar!r} 不属于任何已声明稀有度档位（离线链路会跑不通）",
                )
        if "teammates" not in mock:
            _pc_issue(
                issues,
                "WARN",
                "perception.mock.teammates",
                "未显式声明（单机游戏请写 []，否则会继承默认队友数据串味）",
            )
    return issues


def profile_check(game: str = "", check_all: bool = False, strict: bool = False) -> int:
    """校验游戏档案；返回退出码（0=通过，1=有 ERROR，strict 下 WARN 也算失败）。"""
    targets = []
    if check_all:
        if os.path.isdir(PROFILE_DIR):
            targets = sorted(
                f[:-5] for f in os.listdir(PROFILE_DIR) if f.endswith(".yaml") and not f.startswith("_")
            )
    else:
        targets = [game or active_game()]
    exit_code = 0
    for g in targets:
        issues = profile_check_one(g)
        errors = [i for i in issues if i[0] == "ERROR"]
        warns = [i for i in issues if i[0] == "WARN"]
        status = "✗ FAIL" if errors or (strict and warns) else ("⚠ PASS(有警告)" if warns else "✓ PASS")
        print(f"[{status}] {g}.yaml —— {len(errors)} 错误 / {len(warns)} 警告")
        for level, path_, msg in issues:
            print(f"    {level}: {path_}: {msg}")
        if errors or (strict and warns):
            exit_code = 1
    if not targets:
        print("没有可校验的档案（game_profiles/ 下无 *_*.yaml 之外的文件？）")
        exit_code = 1
    return exit_code


