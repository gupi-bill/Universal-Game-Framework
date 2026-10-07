"""ROADMAP 批次 C 回归测试：#11 BM25 / #12 历史回滚 / #13 调参审计 / #15 复盘配置 / #9 跟踪 / #7 预判模型。"""

import os
import sys
from collections import deque

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import agent  # noqa: E402


# ---------------------------------------------------------------------------
# #11 BM25 检索
# ---------------------------------------------------------------------------
def test_tokenize_ascii_and_cjk():
    toks = agent._tokenize("低血量撤退 low HP")
    assert "low" in toks and "hp" in toks
    assert "血" in toks and "低血" in toks and "血量" in toks


def test_bm25_ranks_more_relevant_first(tmp_path):
    (tmp_path / "rich.md").write_text("boss 打法要点\n" + "boss 走位 " * 30, encoding="utf-8")
    (tmp_path / "thin.md").write_text("顺便提一句 boss", encoding="utf-8")
    out = agent._text_search("boss", str(tmp_path))
    assert "rich.md" in out and "thin.md" in out
    assert out.index("rich.md") < out.index("thin.md"), "高相关文档应排前"


def test_text_search_respects_top_n(tmp_path, monkeypatch):
    monkeypatch.setattr(agent, "_CFG", {"kb": {"search_top_n": 2}})
    for i in range(5):
        (tmp_path / f"f{i}.md").write_text(f"kw{i} 共同词", encoding="utf-8")
    out = agent._text_search("共同词", str(tmp_path))
    assert out.startswith("共找到 2 条结果"), out[:30]


def test_text_search_semantic_hit_without_literal(tmp_path):
    """无字面命中但词素相关（分词命中）也能召回，带相关度标注。"""
    (tmp_path / "a.md").write_text("低血量时应当立即撤退", encoding="utf-8")
    out = agent._text_search("血量", str(tmp_path))
    assert "a.md" in out


def test_kb_search_output_contract(tmp_path, monkeypatch):
    """输出契约不回退：命中带'共找到'，未命中返回'未找到相关内容'。"""
    monkeypatch.setattr(agent, "KB_DIR", str(tmp_path))
    (tmp_path / "x.md").write_text("战术内容", encoding="utf-8")
    hit = agent.kb_search("战术")
    assert agent.kb_is_hit(hit) and hit.startswith("共找到")
    miss = agent.kb_search("不存在的关键词zzz")
    assert miss == "未找到相关内容"


def test_canonical_tactics_survive_polluted_kb(tmp_path):
    """知识库被大量含关键词的噪声文件污染时，权威 tactics.md 不能被 Top-N 挤出。"""
    (tmp_path / "tactics.md").write_text(
        "# 战术知识\n- 战术: 低血量撤退（适用条件：血量低于四成）", encoding="utf-8"
    )
    for i in range(12):
        (tmp_path / f"video_tactic_{i}.md").write_text(
            f"# 视频学习战术 {i}\n## 战术列表\n战术 战术 战术 战术", encoding="utf-8"
        )
    out = agent._text_search("战术", str(tmp_path))
    assert "tactics.md" in out
    tactics = agent.extract_tactics(out)
    assert any(tag == "retreat" for tag, _ in tactics), "决策闭环必须能从检索结果提取到种子战术"


# ---------------------------------------------------------------------------
# #12 知识库历史与回滚
# ---------------------------------------------------------------------------
def test_kb_history_and_rollback(tmp_path, monkeypatch):
    monkeypatch.setattr(agent, "KB_DIR", str(tmp_path))
    game = "histgame"
    agent.kb_write("t1", "V1", game)  # 文件不存在 → 无快照
    agent.kb_append("t1", "V2", game)  # 快照 V1
    agent.kb_write("t1", "V3", game)  # 快照 V1+V2
    h = agent.kb_history("t1", game)
    assert "2 条历史修订" in h
    msg = agent.kb_rollback("t1", 1, game)  # 回到最新快照 = V1+V2
    assert "已回滚" in msg
    p = agent.kb_resolve("t1", game)
    with open(p, encoding="utf-8") as f:
        content = f.read()
    assert "V1" in content and "V2" in content and "V3" not in content
    # 回滚动作本身也留了快照 → 历史 +1
    assert "3 条历史修订" in agent.kb_history("t1", game)


def test_kb_rollback_out_of_range(tmp_path, monkeypatch):
    monkeypatch.setattr(agent, "KB_DIR", str(tmp_path))
    agent.kb_write("t2", "A", "g2")
    agent.kb_write("t2", "B", "g2")  # 1 条快照
    assert "超出范围" in agent.kb_rollback("t2", 99, "g2")
    assert "暂无历史修订" in agent.kb_rollback("nofile", 1, "g2")


def test_kb_history_revisions_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(agent, "KB_DIR", str(tmp_path))
    monkeypatch.setattr(agent, "_CFG", {"kb": {"history_revisions": 3, "history_max_kb": 256}})
    for i in range(6):
        agent.kb_write("t3", f"版本{i}", "g3")
    p = agent.kb_resolve("t3", "g3")
    rows = agent._kb_history_rows(p)
    assert len(rows) == 3, "历史应滚动保留最近 3 条"


def test_kb_history_cli(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(agent, "KB_DIR", str(tmp_path))
    agent.kb_write("t4", "x", "g4")
    agent.kb_write("t4", "y", "g4")
    assert agent.main(["--game", "g4", "kb", "history", "t4"]) == 0
    out = capsys.readouterr().out
    assert "历史修订" in out


# ---------------------------------------------------------------------------
# #13 调参审计与人工锁定
# ---------------------------------------------------------------------------
_FAST_TUNE_CFG = {
    "agent": {"tune_locked": [], "report_every": 0},
    "combat": {"retreat_ratio": 1.0},
    "predictor": {"confidence_threshold": 0.65},
}


def test_tune_audit_trail(tmp_path, monkeypatch):
    """每次变更都留审计：旧值 → 新值 → 依据。"""
    monkeypatch.setattr(agent, "TUNED_PATH", str(tmp_path / "tuned.yaml"))
    monkeypatch.setattr(agent, "_CFG", dict(_FAST_TUNE_CFG))
    agent.auto_tuner_reset()
    msg = agent.auto_tune(hits=0, attempts=10, deaths_extra=2)
    assert "retreat_ratio" in msg
    o = agent._read_yaml(agent.TUNED_PATH)
    audit = o.get("_audit") or []
    assert audit, "应留下审计记录"
    a = audit[0]
    assert a["param"] == "combat.retreat_ratio"
    assert a["old"] == 1.0 and a["new"] == 0.8 and "死亡" in a["reason"]
    assert "审计" in agent.auto_tuner_status()


def test_tune_lock_skips_param(tmp_path, monkeypatch):
    """锁定参数不被调参覆盖，且返回信息留痕。"""
    cfg = dict(_FAST_TUNE_CFG)
    cfg["agent"] = {"tune_locked": ["combat.retreat_ratio"]}
    monkeypatch.setattr(agent, "TUNED_PATH", str(tmp_path / "tuned.yaml"))
    monkeypatch.setattr(agent, "_CFG", cfg)
    agent.auto_tuner_reset()
    msg = agent.auto_tune(hits=1, attempts=10, deaths_extra=1)
    assert "锁定" in msg
    o = agent._read_yaml(agent.TUNED_PATH)
    assert (o.get("combat") or {}).get("retreat_ratio") is None, "锁定参数不得被写入"
    # 未锁定的 confidence 照常调整并留审计
    assert (o.get("predictor") or {}).get("confidence_threshold") is not None
    assert any(a["param"] == "predictor.confidence_threshold" for a in (o.get("_audit") or []))
    assert "人工锁定" in agent.auto_tuner_status()


# ---------------------------------------------------------------------------
# #15 复盘可配置
# ---------------------------------------------------------------------------
def test_review_disabled_by_config(monkeypatch):
    monkeypatch.setattr(agent, "_CFG", {"review": {"enabled": False}})
    state = {"entities": [{"rarity": "Super"}], "teammates": [{"n": 1}]}
    assert agent.should_review(state) is False


def test_review_triggers_configurable(monkeypatch):
    monkeypatch.setattr(
        agent,
        "_CFG",
        {
            "review": {"enabled": True, "trigger_boss": False, "trigger_team": True},
            "predictor": {"rarity_boss": ["Super"], "rarity_highest_boss": ["Unique"]},
        },
    )
    assert agent.should_review({"entities": [{"rarity": "Super"}], "teammates": []}) is False
    assert agent.should_review({"entities": [], "teammates": [{"name": "a"}]}) is True


def test_review_boss_rarity_from_profile(monkeypatch):
    """稀有度档来自档案而非硬编码：自定义档位名也认。"""
    monkeypatch.setattr(
        agent,
        "_CFG",
        {
            "review": {"enabled": True, "trigger_boss": True, "trigger_team": True},
            "predictor": {"rarity_boss": ["Mothership"], "rarity_highest_boss": []},
        },
    )
    assert agent.should_review({"entities": [{"rarity": "mothership"}], "teammates": []}) is True
    assert agent.should_review({"entities": [{"rarity": "Super"}], "teammates": []}) is False


def test_review_custom_template_and_structured_fields(tmp_path, monkeypatch):
    monkeypatch.setattr(agent, "KB_DIR", str(tmp_path))
    monkeypatch.setattr(
        agent,
        "_CFG",
        {
            "review": {
                "enabled": True,
                "trigger_boss": True,
                "trigger_team": True,
                "template": "自定义复盘 {outcome} 套装={set}",
            },
            "kb": {"search_top_n": 5, "history_revisions": 5, "history_max_kb": 256},
            "predictor": {"rarity_boss": ["Super"], "rarity_highest_boss": []},
        },
    )
    state = {
        "player": {"petal_set": "tank"},
        "entities": [{"raw_id": "m", "rarity": "Super"}],
        "teammates": [],
    }
    msg = agent.review_round(False, "测试", state)
    assert "已写入知识库" in msg
    gdir = tmp_path / agent.active_game()
    files = sorted(gdir.glob("review_*.md"))
    assert files
    with open(files[-1], encoding="utf-8") as f:
        c = f.read()
    assert c.startswith("自定义复盘 死亡 套装=tank")
    assert "## 结构化字段" in c and "killer_entities:" in c


# ---------------------------------------------------------------------------
# #9 实体跟踪 ID 稳定性
# ---------------------------------------------------------------------------
def _mk_predictor_with(trackers):
    p = agent.Predictor()
    uids = []
    for i, (rid, hist) in enumerate(trackers, 1):
        uid = f"{rid}_{i}"
        tk = agent._Tracker(rid, "Common", "monster")
        tk.history = deque(hist, maxlen=max(10, len(hist)))
        tk.last_seen = hist[-1]["t"]
        p._t[uid] = tk
        uids.append(uid)
    return p, uids


def test_match_prefers_predicted_position_on_cross(monkeypatch):
    """交叉走位：按最近距离会张冠李戴，按预测位置 + 方向一致性才对。"""
    monkeypatch.setattr(agent, "_CFG", {"predictor": {"match_max_dist": 400}})
    now = 100.0
    t1 = [
        {"t": now - 0.2, "x": 35.0, "y": 0.0},
        {"t": now - 0.1, "x": 40.0, "y": 0.0},
        {"t": now, "x": 45.0, "y": 0.0},
    ]  # 右行 v=+50
    t2 = [
        {"t": now - 0.2, "x": 65.0, "y": 0.0},
        {"t": now - 0.1, "x": 60.0, "y": 0.0},
        {"t": now, "x": 55.0, "y": 0.0},
    ]  # 左行 v=-50
    p, uids = _mk_predictor_with([("bee", t1), ("bee", t2)])
    dets = [
        {"raw_id": "bee", "rarity": "Common", "role": "monster", "x": 40.0, "y": 0.0},
        {"raw_id": "bee", "rarity": "Common", "role": "monster", "x": 60.0, "y": 0.0},
    ]
    assign = p._match(dets, now + 0.1)
    assert assign[0] == uids[1], "左行检测点应归左行跟踪器"
    assert assign[1] == uids[0], "右行检测点应归右行跟踪器"


def test_match_max_dist_opens_new_track(monkeypatch):
    """瞬移/重生级别的跳变不误挂旧轨，宁开新轨。"""
    monkeypatch.setattr(agent, "_CFG", {"predictor": {"match_max_dist": 50}})
    now = 100.0
    t1 = [{"t": now, "x": 0.0, "y": 0.0}]
    p, _uids = _mk_predictor_with([("bee", t1)])
    dets = [{"raw_id": "bee", "rarity": "Common", "role": "monster", "x": 900.0, "y": 900.0}]
    assert p._match(dets, now) == {}


def test_role_flip_penalized(monkeypatch):
    """敌↔友角色跳变是强误配信号：优先分给角色一致的跟踪器。"""
    monkeypatch.setattr(agent, "_CFG", {"predictor": {"match_max_dist": 400}})
    now = 100.0
    t_enemy = [{"t": now, "x": 50.0, "y": 0.0}]
    t_ally = [{"t": now, "x": 52.0, "y": 0.0}]
    p = agent.Predictor()
    p._t["e1"] = agent._Tracker("p1", "Common", "player_enemy")
    p._t["e1"].history = deque(t_enemy, maxlen=10)
    p._t["a1"] = agent._Tracker("p1", "Common", "player_ally")
    p._t["a1"].history = deque(t_ally, maxlen=10)
    dets = [{"raw_id": "p1", "rarity": "Common", "role": "player_ally", "x": 51.0, "y": 0.0}]
    assign = p._match(dets, now + 0.05)
    assert assign[0] == "a1", "即使敌人跟踪器距离更近，角色一致优先"


def test_predictor_update_still_tracks_normal_motion():
    """常规单实体跟踪不因匹配升级而回归。"""
    p = agent.Predictor()
    for i in range(5):
        p.update([{"raw_id": "hornet", "rarity": "Common", "x": 100 + i * 40, "y": 200}])
    preds = p.all_entities()
    assert len(preds) == 1 and preds[0]["raw_id"] == "hornet"


# ---------------------------------------------------------------------------
# #7 预判模型升级（线性 / 恒加速度 / 圆周 + auto）
# ---------------------------------------------------------------------------
import math  # noqa: E402


def _mk_tracker(history):
    tk = agent._Tracker("t", "Common", "monster")
    tk.history = deque(history, maxlen=max(4, len(history)))
    return tk


def test_accel_model_beats_linear_on_accelerating():
    """恒加速轨迹（x=500t²）：accel 模型的 1.2s 外推误差应远小于线性。"""
    hist = [{"t": i * 0.1, "x": 500 * (i * 0.1) ** 2, "y": 0.0} for i in range(12)]
    tk = _mk_tracker(hist)
    lin = tk._fit_linear(1.2)
    acc = tk._fit_accel(1.2)
    assert acc is not None and acc["model"] == "accel"
    truth = 500 * (hist[-1]["t"] + 1.2) ** 2
    assert abs(acc["x"] - truth) < abs(lin["x"] - truth)
    assert abs(acc["x"] - truth) < 50, f"accel 误差应很小，实际 {abs(acc['x'] - truth):.1f}"


def test_circular_model_beats_linear_on_circle():
    """圆周轨迹：circular 模型沿弧外推，误差应显著小于线性切线外推。"""
    R, w = 150.0, 1.5
    hist = [
        {"t": i * 0.05, "x": 500 + R * math.cos(w * i * 0.05), "y": 500 + R * math.sin(w * i * 0.05)}
        for i in range(40)
    ]
    tk = _mk_tracker(hist)
    circ = tk._fit_circular(0.5)
    lin = tk._fit_linear(0.5)
    assert circ is not None and circ["model"] == "circular"
    t_next = hist[-1]["t"] + 0.5
    tx = 500 + R * math.cos(w * t_next)
    ty = 500 + R * math.sin(w * t_next)
    err_c = math.hypot(circ["x"] - tx, circ["y"] - ty)
    err_l = math.hypot(lin["x"] - tx, lin["y"] - ty)
    assert err_c < err_l, f"圆周误差 {err_c:.1f} 应小于线性 {err_l:.1f}"
    assert err_c < 15, f"圆周模型误差应很小，实际 {err_c:.1f}"


def test_circular_rejects_straight_line():
    """直线轨迹不应被误判为圆周。"""
    hist = [{"t": i * 0.1, "x": 100 + i * 30.0, "y": 50.0} for i in range(12)]
    tk = _mk_tracker(hist)
    assert tk._fit_circular(1.0) is None


def test_predict_output_has_model_field():
    hist = [{"t": i * 0.1, "x": 100 + i * 10.0, "y": 50.0} for i in range(8)]
    tk = _mk_tracker(hist)
    out = tk.predict()
    assert out and out["model"] in ("linear", "accel", "circular")
    for key in ("raw_id", "category", "threat_score", "confidence", "prediction_trusted"):
        assert key in out


def test_model_force_config(monkeypatch):
    """predictor.model 强制指定时按指定模型输出。"""
    monkeypatch.setattr(
        agent,
        "_CFG",
        {
            "predictor": {
                "model": "linear",
                "min_frames": 3,
                "predict_seconds": 1.0,
                "frame_full_frames": 8,
                "speed_ref": 2000,
                "speed_penalty_floor": 0.3,
                "jitter_floor": 0.35,
                "confidence_threshold": 0.0,
                "threat": {},
                "history_maxlen": 10,
            }
        },
    )
    hist = [{"t": i * 0.1, "x": 500 * (i * 0.1) ** 2, "y": 0.0} for i in range(12)]
    tk = _mk_tracker(hist)
    out = tk.predict()
    assert out["model"] == "linear"


def test_auto_selects_best_model_on_circle(monkeypatch):
    """auto 模式在圆周轨迹上应选中 circular（回测残差最小）。"""
    monkeypatch.setattr(
        agent,
        "_CFG",
        {
            "predictor": {
                "model": "auto",
                "min_frames": 3,
                "predict_seconds": 0.5,
                "frame_full_frames": 8,
                "speed_ref": 2000,
                "speed_penalty_floor": 0.3,
                "jitter_floor": 0.35,
                "confidence_threshold": 0.0,
                "circular_min_frames": 8,
                "circular_min_radius": 20,
                "circular_min_omega": 0.3,
                "accel_max": 2000,
                "threat": {},
                "history_maxlen": 40,
            }
        },
    )
    R, w = 150.0, 1.5
    hist = [
        {"t": i * 0.05, "x": 500 + R * math.cos(w * i * 0.05), "y": 500 + R * math.sin(w * i * 0.05)}
        for i in range(20)
    ]
    tk = _mk_tracker(hist)
    out = tk.predict()
    assert out["model"] == "circular"
    assert out["prediction_trusted"] is True, "圆周模型不应再被直线度惩罚压死"
