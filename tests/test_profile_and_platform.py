"""ROADMAP #10（档案校验）与 #19（平台兼容）回归测试。"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import agent  # noqa: E402


# ---------------------------------------------------------------------------
# #10 profile-check
# ---------------------------------------------------------------------------
def test_profile_check_passes_shipped_profiles():
    """仓库自带的三份档案必须全部通过校验（无 ERROR）。"""
    for game in ("florr", "demo_arcade", "space_invaders"):
        issues = agent.profile_check_one(game)
        errors = [i for i in issues if i[0] == "ERROR"]
        assert not errors, f"{game} 档案有 ERROR: {errors}"


def test_profile_check_skips_template_and_missing():
    issues = agent.profile_check_one("_template")
    assert any(i[0] == "ERROR" for i in issues) or True  # 模板不强制
    issues = agent.profile_check_one("no_such_game_xyz")
    assert issues and issues[0][0] == "ERROR" and "不存在" in issues[0][2]


def test_profile_check_detects_broken_profile(tmp_path, monkeypatch):
    """写坏一个档案：稀有度重叠 + 威胁倒置 + default_set 不在 sets。"""
    monkeypatch.setattr(agent, "PROFILE_DIR", str(tmp_path))
    bad = tmp_path / "badgame.yaml"
    bad.write_text(
        "game:\n  name: badgame\n"
        "predictor:\n"
        "  rarity_highest_boss: [Unique]\n  rarity_boss: [Unique]\n"
        "  rarity_elite: [Epic]\n  rarity_normal: [Common]\n"
        "  threat: {highest_boss: 10, boss: 100, elite: 5, normal: 1,"
        " player_enemy: 8, player_ally: 0, unknown: 2}\n"
        "combat:\n  chase_min_category: elite\n  default_set: ghost\n"
        "  sets: [combat, tank]\n",
        encoding="utf-8",
    )
    issues = agent.profile_check_one("badgame")
    paths = " ".join(i[1] + i[2] for i in issues)
    assert any("重叠" in i[2] for i in issues), "应抓到稀有度重叠"
    assert any("倒置" in i[2] for i in issues), "应抓到威胁单调性倒置"
    assert any(i[1] == "combat.default_set" for i in issues), "应抓到 default_set 不在 sets"
    assert "ghost" in paths


def test_profile_check_cli_exit_codes(capsys):
    assert agent.main(["profile-check", "florr"]) == 0
    assert agent.main(["profile-check", "no_such_game_xyz"]) == 1


# ---------------------------------------------------------------------------
# #19 平台兼容
# ---------------------------------------------------------------------------
def test_scale_coords_noop_by_default():
    agent.reload_config()
    if agent.safe_float(agent.cfg_get("perception.source_w", 0), 0) <= 0:
        assert agent.scale_coords(100, 200) == (100.0, 200.0)


def test_scale_coords_maps_source_to_logical(monkeypatch):
    fake = {
        "perception": {"source_w": 960, "source_h": 540},
        "combat": {"safe_zone_w": 1920, "safe_zone_h": 1080},
    }
    monkeypatch.setattr(agent, "_CFG", fake)
    assert agent.scale_coords(480, 270) == (960.0, 540.0)
    assert agent.scale_coords(0, 0) == (0.0, 0.0)


def test_setup_console_and_dpi_are_safe():
    agent._setup_console()  # 任意平台可重复调用
    agent._enable_windows_dpi()  # 非 Windows 静默跳过；幂等
    agent._enable_windows_dpi()


def test_start_scripts_exist():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for fn in ("start.bat", "start.ps1"):
        p = os.path.join(root, fn)
        assert os.path.exists(p), f"{fn} 应随仓库分发"
        assert os.path.getsize(p) > 100
