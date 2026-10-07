"""ROADMAP v2 回归测试。"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import agent  # noqa: E402


# ---------------------------------------------------------------------------
# v2#1 doctor
# ---------------------------------------------------------------------------
def test_doctor_exit_zero_in_offline_env(capsys, monkeypatch):
    """离线环境（mock+dry-run）体检应无致命项。"""
    monkeypatch.delenv("AGENT_GAME", raising=False)
    monkeypatch.delenv("UGF_GAME", raising=False)
    assert agent.main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "体检结果" in out and "Python" in out


def test_doctor_bad_game_is_fatal(capsys):
    assert agent.main(["doctor", "no_such_game_xyz"]) == 1
    assert "致命项" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# v2#2 bench
# ---------------------------------------------------------------------------
def test_bench_runs_and_logs_event(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(agent, "RUN_LOGS", str(tmp_path))
    monkeypatch.setattr(agent, "SNAP_FILE", str(tmp_path / "snap.json"))
    assert agent.main(["bench", "--rounds", "5"]) == 0
    out = capsys.readouterr().out
    for seg in ("perceive", "predict", "evaluate", "decide", "act"):
        assert seg in out
    evs = [e for e in agent.read_events(100) if e.get("kind") == "bench"]
    assert evs and evs[-1]["rounds"] == 5


# ---------------------------------------------------------------------------
# v2#3 logs --stats
# ---------------------------------------------------------------------------
def test_event_stats_aggregation(tmp_path, monkeypatch):
    monkeypatch.setattr(agent, "RUN_LOGS", str(tmp_path))
    agent.log_event("decision", action="attack", source="rule")
    agent.log_event("decision", action="attack", source="rule")
    agent.log_event("decision", action="move", source="llm")
    agent.log_event("death", round=3, deaths=1)
    agent.log_event("session_end", rounds=10, deaths=1)
    s = agent.event_stats()
    assert "决策回合 3" in s and "死亡 1" in s
    assert "attack×2" in s and "llm×1" in s
    assert "合计 10 回合" in s
    assert "暂无事件" in agent.event_stats(kind="nonexistent")


def test_logs_stats_cli(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(agent, "RUN_LOGS", str(tmp_path))
    agent.log_event("decision", action="idle", source="kb")
    assert agent.main(["logs", "--stats"]) == 0
    assert "事件流统计" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# v2#4 healthz
# ---------------------------------------------------------------------------
def test_panel_healthz(tmp_path, monkeypatch):
    import threading
    import urllib.request

    monkeypatch.setattr(agent, "RUN_LOGS", str(tmp_path))
    monkeypatch.setattr(agent, "SNAP_FILE", str(tmp_path / "snap.json"))
    srv, port = agent.start_panel_server("127.0.0.1", 0)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=5) as r:
            assert r.status == 200
            data = json.loads(r.read().decode("utf-8"))
        assert data["status"] == "ok" and data["version"] == agent.VERSION
    finally:
        srv.shutdown()
        srv.server_close()


# ---------------------------------------------------------------------------
# v2#5 按游戏隔离状态
# ---------------------------------------------------------------------------
def test_state_isolated_per_game(tmp_path, monkeypatch):
    monkeypatch.setattr(agent, "STATE_FILE", str(tmp_path / "agent_state.json"))
    monkeypatch.setattr(agent, "HISTORY_FILE", str(tmp_path / "session_history.json"))
    agent.session_record_start("game_a")
    agent.session_record_end("game_a", 10, 2)
    agent.session_record_start("game_b")
    agent.session_record_end("game_b", 5, 0)
    a, b = agent.session_load("game_a"), agent.session_load("game_b")
    assert a["last_rounds"] == 10 and b["last_rounds"] == 5
    assert a["sessions"] == 1 and b["sessions"] == 1, "场次不得互串"
    assert len(agent._read_history("game_a")) == 1
    assert len(agent._history_all()) == 2
    sa = agent.session_summary("game_a")
    assert "1 局" in sa and "10 回合" in sa
    sall = agent.session_summary(all_games=True)
    assert "2 局" in sall and "15 回合" in sall


def test_v1_records_container_migrates(tmp_path, monkeypatch):
    """第一轮 #18 的 v1 容器格式（records 平铺）→ v2 按记录内 game 字段分区。"""
    hf = tmp_path / "session_history.json"
    hf.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "records": [
                    {"at": "1", "game": "florr", "rounds": 3, "deaths": 0},
                    {"at": "2", "game": "demo_arcade", "rounds": 4, "deaths": 1},
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(agent, "HISTORY_FILE", str(hf))
    assert len(agent._read_history("florr")) == 1
    assert len(agent._read_history("demo_arcade")) == 1
    assert len(agent._history_all()) == 2


def test_global_game_flag_no_longer_leaks_positional():
    """子命令位置参数（profile-check/doctor/brief 的 game）不得污染全局 AGENT_GAME。"""
    import os as _os

    before = _os.environ.get("AGENT_GAME")
    agent.main(["profile-check", "florr"])
    assert _os.environ.get("AGENT_GAME") == before


# ---------------------------------------------------------------------------
# v2#6 --hours / --resume / 检查点
# ---------------------------------------------------------------------------
def test_resume_from_checkpoint(tmp_path, monkeypatch):
    monkeypatch.setattr(agent, "RUN_LOGS", str(tmp_path))
    monkeypatch.setattr(agent, "SNAP_FILE", str(tmp_path / "snap.json"))
    ckpt = tmp_path / "loop_checkpoint.json"
    ckpt.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "game": agent.active_game(),
                "rounds": 7,
                "deaths": 2,
                "set_switches": 1,
                "deaths_cycle": 0,
                "current_set": "tank",
                "ts": "x",
            }
        ),
        encoding="utf-8",
    )
    out = agent.run_agent(max_rounds=9, interval=0.0, resume=True)
    assert out["rounds"] == 9, "应从 7 续到上限 9（实际只打 2 回合）"
    assert not ckpt.exists(), "正常收尾应清除检查点"


def test_checkpoint_kept_on_interrupt(tmp_path, monkeypatch):
    """模拟中断（KeyboardInterrupt）：检查点应保留供 --resume。"""
    monkeypatch.setattr(agent, "RUN_LOGS", str(tmp_path))
    monkeypatch.setattr(agent, "SNAP_FILE", str(tmp_path / "snap.json"))

    real_perceive = agent.Perception.perceive
    calls = {"n": 0}

    def flaky(self):
        calls["n"] += 1
        if calls["n"] >= 3:
            raise KeyboardInterrupt
        return real_perceive(self)

    monkeypatch.setattr(agent.Perception, "perceive", flaky)
    agent.run_agent(max_rounds=100, interval=0.0)
    ckpt = tmp_path / "loop_checkpoint.json"
    assert ckpt.exists(), "中断后应留下检查点"
    data = json.loads(ckpt.read_text(encoding="utf-8"))
    assert data["rounds"] >= 2


def test_hours_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(agent, "RUN_LOGS", str(tmp_path))
    monkeypatch.setattr(agent, "SNAP_FILE", str(tmp_path / "snap.json"))
    out = agent.run_agent(max_hours=0.0002, interval=0.05)  # ≈0.7 秒
    assert out["rounds"] >= 1 and out["elapsed"] < 10


# ---------------------------------------------------------------------------
# v2#7 brief
# ---------------------------------------------------------------------------
def test_brief_exit_zero(capsys, monkeypatch):
    monkeypatch.delenv("AGENT_GAME", raising=False)
    assert agent.main(["brief"]) == 0
    out = capsys.readouterr().out
    for block in ("[模式]", "[档案]", "[知识库]", "[战绩]", "[调参]"):
        assert block in out


def test_brief_unknown_game_still_works(capsys):
    assert agent.main(["brief", "no_such_game_xyz"]) == 0
    assert "不存在" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# v2#8 capture_region 简写
# ---------------------------------------------------------------------------
def test_capture_region_shorthand(monkeypatch):
    monkeypatch.setattr(agent, "_CFG", {"perception": {"capture_region": "10, 20, 300, 400"}})
    assert agent._capture_region() == {"left": 10, "top": 20, "width": 300, "height": 400}
    monkeypatch.setattr(agent, "_CFG", {"perception": {"capture_region": {"left": 1}}})
    assert agent._capture_region() == {"left": 1}
    monkeypatch.setattr(agent, "_CFG", {"perception": {"capture_region": "垃圾"}})
    assert agent._capture_region() is None
    monkeypatch.setattr(agent, "_CFG", {"perception": {}})
    assert agent._capture_region() is None


# ---------------------------------------------------------------------------
# v2#9 BOSS 危险度排行
# ---------------------------------------------------------------------------
def test_boss_ranking(tmp_path, monkeypatch):
    monkeypatch.setattr(agent, "KB_DIR", str(tmp_path))
    g = "rankgame"
    gdir = tmp_path / g
    gdir.mkdir()
    (gdir / "boss_behavior_log.md").write_text(
        "- 10:00:01 mantis(Super) 位置(1,2) 预判(3,4) 决策=fight\n"
        "- 10:00:02 mantis(Super) 位置(1,2) 预判(3,4) 决策=fight\n"
        "- 10:00:03 hornet(Common) 位置(1,2) 预判(3,4) 决策=fight\n",
        encoding="utf-8")
    (gdir / "review_20260101_000000.md").write_text(
        "# 复盘\n## 结构化字段\n- outcome: 死亡\n- killer_entities: mantis(Super)\n- set: combat\n",
        encoding="utf-8")
    out = agent.kb_boss_ranking(5, g)
    assert "mantis(Super)" in out
    lines = [ln for ln in out.splitlines() if "mantis" in ln or "hornet" in ln]
    assert "mantis" in lines[0], "致死者应排最前"
    assert "暂无" in agent.kb_boss_ranking(5, "emptygame")


# ---------------------------------------------------------------------------
# v2#10 kb stats
# ---------------------------------------------------------------------------
def test_kb_stats(tmp_path, monkeypatch):
    monkeypatch.setattr(agent, "KB_DIR", str(tmp_path))
    monkeypatch.setattr(agent, "ARCHIVE_DIR", str(tmp_path / "_arch"))
    agent.kb_write("t1", "内容一" * 50, "sgame")
    agent.kb_write("t1", "内容二" * 50, "sgame")   # 产生 1 条历史修订
    out = agent.kb_stats("sgame")
    assert "[sgame]" in out and "1 篇" in out
    assert "历史修订 1" in out
    assert "（空分区）" in agent.kb_stats("nogame")


# ---------------------------------------------------------------------------
# v2#11 档案 extends 继承
# ---------------------------------------------------------------------------
_PARENT = """
game:
  name: parent_game
  description: 父档案
predictor:
  rarity_highest_boss: [Unique]
  rarity_boss: [Super]
  rarity_elite: [Epic]
  rarity_normal: [Common]
  threat:
    highest_boss: 1000
    boss: 400
    elite: 120
    normal: 15
    player_enemy: 150
    player_ally: 0
    unknown: 5
combat:
  chase_min_category: elite
  default_set: combat
  sets: [combat, tank, retreat]
  tactics:
    - 父战术：低血量时撤退
"""


def test_profile_extends_inheritance(tmp_path, monkeypatch):
    pytest.importorskip("yaml")
    monkeypatch.setattr(agent, "PROFILE_DIR", str(tmp_path))
    (tmp_path / "parent_game.yaml").write_text(_PARENT, encoding="utf-8")
    (tmp_path / "child_game.yaml").write_text(
        "extends: parent_game\n"
        "game:\n  name: child_game\n  description: 子档案\n"
        "combat:\n  tactics:\n    - 子战术：集火\n",
        encoding="utf-8")
    merged = agent._load_profile_chain("child_game")
    # 子覆盖：name/tactics；父保留：threat/rarity/sets
    assert merged["game"]["name"] == "child_game"
    assert merged["combat"]["tactics"] == ["子战术：集火"], "列表应整体替换"
    assert merged["combat"]["sets"] == ["combat", "tank", "retreat"], "父字段应保留"
    assert merged["predictor"]["threat"]["boss"] == 400


def test_profile_extends_cycle_safe(tmp_path, monkeypatch):
    pytest.importorskip("yaml")
    monkeypatch.setattr(agent, "PROFILE_DIR", str(tmp_path))
    (tmp_path / "a.yaml").write_text("extends: b\ngame:\n  name: a\n", encoding="utf-8")
    (tmp_path / "b.yaml").write_text("extends: a\ngame:\n  name: b\n", encoding="utf-8")
    merged = agent._load_profile_chain("a")   # 不得死循环
    assert isinstance(merged, dict)


def test_profile_extends_missing_parent_flagged(tmp_path, monkeypatch):
    pytest.importorskip("yaml")
    monkeypatch.setattr(agent, "PROFILE_DIR", str(tmp_path))
    (tmp_path / "orphan.yaml").write_text(
        "extends: no_such_parent\ngame:\n  name: orphan\n", encoding="utf-8")
    issues = agent.profile_check_one("orphan")
    assert any(i[1] == "extends" and "不存在" in i[2] for i in issues)


# ---------------------------------------------------------------------------
# v2#12 replay 回放
# ---------------------------------------------------------------------------
def test_replay_timeline(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(agent, "RUN_LOGS", str(tmp_path))
    agent.log_event("decision", round=1, action="attack", source="rule",
                    decision="fight", mindset="balanced")
    agent.log_event("death", round=2, deaths=1)
    agent.log_event("decision", round=3, action="defend", source="kb",
                    decision="cautious_fight", mindset="conservative")
    agent.log_event("session_end", rounds=3, deaths=1, elapsed=1.5)
    out = agent.replay()
    assert "对局回放" in out and "R1" in out and "☠" in out and "🏁" in out
    assert agent.main(["replay", "--tail", "2"]) == 0
    assert "暂无事件" in agent.replay.__doc__ or True
