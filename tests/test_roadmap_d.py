"""ROADMAP 批次 D 回归测试：#6 感知后端 / #14 视频学习 / #17 面板 / #20 故障注入 / #4 类型。"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import agent  # noqa: E402


# ---------------------------------------------------------------------------
# #6 感知后端插件化
# ---------------------------------------------------------------------------
def test_backend_registry_and_fallback(monkeypatch):
    """未知后端回落 mock；auto 在 dry-run 下走 mock。"""
    monkeypatch.setenv("UGF_DRY_RUN", "1")
    monkeypatch.setenv("UGF_PERCEPTION_BACKEND", "not_a_backend")
    p = agent.Perception()
    assert p.backend() == "mock"
    assert "error" not in p.frame()
    monkeypatch.setenv("UGF_PERCEPTION_BACKEND", "auto")
    assert p.backend() == "mock"


def test_local_backend_requires_config(monkeypatch):
    monkeypatch.setenv("UGF_PERCEPTION_BACKEND", "local")
    frame = agent.Perception()._local_frame()
    assert "error" in frame and "model_path" in frame["error"]


def test_local_backend_missing_model_file(monkeypatch):
    monkeypatch.setenv("UGF_PERCEPTION_BACKEND", "local")
    monkeypatch.setattr(agent, "_CFG", {"perception": {"local": {"model_path": "/nonexistent/x.onnx"}}})
    frame = agent.Perception()._local_frame()
    assert "error" in frame and "不存在" in frame["error"]


def test_template_backend_requires_dir(monkeypatch):
    monkeypatch.setenv("UGF_PERCEPTION_BACKEND", "template")
    frame = agent.Perception()._template_frame()
    assert "error" in frame and "template.dir" in frame["error"]


def test_player_stub_default_and_override(monkeypatch):
    stub = agent._player_stub()
    assert stub["alive"] is True and stub["hp"] == 100
    monkeypatch.setattr(
        agent,
        "_CFG",
        {
            "perception": {"player_stub": {"alive": True, "hp": 55}},
            "combat": {"safe_zone_w": 1920, "safe_zone_h": 1080, "default_set": "combat"},
        },
    )
    assert agent._player_stub()["hp"] == 55


def test_iou_math():
    assert agent._iou_c(0, 0, 10, 10, 0, 0, 10, 10) == pytest.approx(1.0)
    assert agent._iou_c(0, 0, 10, 10, 100, 100, 10, 10) == 0.0
    half = agent._iou_c(0, 0, 10, 10, 5, 0, 10, 10)
    assert 0 < half < 1


def test_parse_yolo_output():
    np = pytest.importorskip("numpy")
    # [1, 4+2类, 3锚点]：锚点0 高置信 class1，锚点1 低置信，锚点2 高置信 class0
    arr = np.zeros((1, 6, 3), dtype="float32")
    arr[0, :4, 0] = [320, 240, 40, 40]  # cx,cy,w,h（640 输入尺度）
    arr[0, 5, 0] = 0.9  # class1 得分
    arr[0, :4, 1] = [100, 100, 20, 20]
    arr[0, 4, 1] = 0.2  # 低于阈值
    arr[0, :4, 2] = [330, 245, 40, 40]  # 与锚点0 高度重叠 → NMS 吃掉
    arr[0, 4, 2] = 0.8
    ents = agent._parse_yolo_output([arr], 1920, 1080, 640, 0.5, ["hornet", "mantis"], {"mantis": "Super"})
    assert len(ents) == 1, "低置信被过滤、重叠框被 NMS"
    e = ents[0]
    assert e["raw_id"] == "mantis" and e["rarity"] == "Super"
    assert e["x"] == pytest.approx(320 / 640 * 1920, abs=1)
    assert e["y"] == pytest.approx(240 / 640 * 1080, abs=1)


def test_parse_yolo_output_transposed_and_empty():
    np = pytest.importorskip("numpy")
    assert agent._parse_yolo_output([np.zeros((3, 6), dtype="float32")], 1920, 1080, 640, 0.5, [], {}) == []
    assert agent._parse_yolo_output([np.zeros((0,), dtype="float32")], 100, 100, 640, 0.5, [], {}) == []


# ---------------------------------------------------------------------------
# #14 视频学习增强
# ---------------------------------------------------------------------------
def test_avg_hash_and_hamming(tmp_path):
    """合成纯色 PNG：同色哈希相同，异色汉明距离大。"""
    p1 = agent._write_png(str(tmp_path / "a.png"), 64, 48, (200, 30, 30))
    p2 = agent._write_png(str(tmp_path / "b.png"), 64, 48, (200, 30, 30))
    p3 = agent._write_png(str(tmp_path / "c.png"), 64, 48, (10, 240, 240))
    h1, h2, h3 = agent._avg_hash(p1), agent._avg_hash(p2), agent._avg_hash(p3)
    assert h1 is not None and h1 == h2
    assert isinstance(h3, int)
    assert agent._hamming(h1, h2) == 0


def test_dedup_frames_skips_duplicates(tmp_path, monkeypatch):
    paths = [agent._write_png(str(tmp_path / f"f{i}.png"), 32, 24, (i * 60 % 256, 90, 120)) for i in range(2)]
    dup = agent._write_png(str(tmp_path / "dup.png"), 32, 24, (0, 90, 120))  # 与 f0 同色
    monkeypatch.setattr(agent, "_CFG", {"learn": {"hash_dedup": True, "hash_threshold": 5}})
    kept, skipped = agent._dedup_frames(paths + [dup])
    assert skipped == 1 and len(kept) == 2
    # 关闭去重则全保留
    monkeypatch.setattr(agent, "_CFG", {"learn": {"hash_dedup": False}})
    kept, skipped = agent._dedup_frames(paths + [dup])
    assert skipped == 0 and len(kept) == 3


def test_vote_tactics():
    tactics = [
        "低血量时立即撤退不要恋战",
        "低血量时立即撤退，不要恋战！",  # 与上一条相似 → 2 票
        "集火清理低威胁目标保持场面干净",  # 仅 1 票
        "[未配置 VLM_API_URL / VLM_API_KEY，跳过 VLM]",  # 无效句直接过滤
    ]
    voted = agent._vote_tactics(tactics, min_votes=2)
    assert len(voted) == 1 and "撤退" in voted[0]
    # min_votes=1 时两条都进
    assert len(agent._vote_tactics(tactics, min_votes=1)) == 2


def test_vlm_prompt_override(monkeypatch):
    monkeypatch.setattr(agent, "_CFG", {"llm": {"vlm_prompt": "自定义提示词"}})
    assert agent._vlm_prompt() == "自定义提示词"
    monkeypatch.setattr(agent, "_CFG", {"llm": {"vlm_prompt": ""}})
    assert agent._vlm_prompt() == agent.VLM_PROMPT


def test_learn_from_video_pipeline_offline(tmp_path, monkeypatch):
    """离线全链路：无 VLM 时 kept=0 但产物文件与统计字段齐全。"""
    monkeypatch.setattr(agent, "FRAME_DIR", str(tmp_path / "frames"))
    monkeypatch.setattr(agent, "KB_DIR", str(tmp_path / "kb"))
    res = agent.learn_from_video(None, frame_count=5)
    assert os.path.exists(res["file"])
    assert res["frames"] == 5
    assert res["kept"] == 0 and res["voted"] == 0
    assert "dedup_skipped" in res


# ---------------------------------------------------------------------------
# #17 监控面板
# ---------------------------------------------------------------------------
def test_panel_server_serves_state_and_html(tmp_path, monkeypatch):
    import json as _json
    import threading
    import urllib.request

    monkeypatch.setattr(agent, "RUN_LOGS", str(tmp_path))
    monkeypatch.setattr(agent, "SNAP_FILE", str(tmp_path / "agent_snapshot.json"))
    (tmp_path / "agent_snapshot.json").write_text(
        _json.dumps(
            {
                "round": 7,
                "deaths": 1,
                "game": "florr",
                "decision": "fight",
                "action_source": "rule",
                "threats": [{"name": "mantis", "cat": "boss", "threat": 400, "x": 1, "y": 2}],
            }
        ),
        encoding="utf-8",
    )
    srv, port = agent.start_panel_server("127.0.0.1", 0)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state", timeout=5) as r:
            data = _json.loads(r.read().decode("utf-8"))
        assert data["snapshot"]["round"] == 7
        for key in ("mode", "session", "events", "log_tail", "learning", "tuner"):
            assert key in data
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5) as r:
            html = r.read().decode("utf-8")
        assert "<html" in html.lower() and "/api/state" in html
        # 404 路径
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/nope", timeout=5)
            raise AssertionError("应 404")
        except urllib.error.HTTPError as e:
            assert e.code == 404
    finally:
        srv.shutdown()
        srv.server_close()


def test_panel_state_zero_side_effect(tmp_path, monkeypatch):
    """agent 没跑过（无快照）时面板数据也能安全聚合。"""
    monkeypatch.setattr(agent, "RUN_LOGS", str(tmp_path))
    monkeypatch.setattr(agent, "SNAP_FILE", str(tmp_path / "nonexistent.json"))
    d = agent._panel_state()
    assert d["snapshot"] == {} and isinstance(d["events"], list)


# ---------------------------------------------------------------------------
# #4 类型化
# ---------------------------------------------------------------------------
def test_core_types_exist_and_annotated():
    import typing

    for name in ("FramePayload", "EntityPred", "CombatEval", "ActionDict"):
        td = getattr(agent, name)
        assert issubclass(td, dict), f"{name} 应为 TypedDict"
    hints = typing.get_type_hints(agent.judge_combat)
    assert "CombatEval" in str(hints["return"])
    hints = typing.get_type_hints(agent.llm_decide)
    assert "ActionDict" in str(hints["return"])
