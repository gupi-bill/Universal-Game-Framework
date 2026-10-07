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
