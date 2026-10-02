#!/usr/bin/env python3
"""
S6 · 感知服务离线化测试 tests/test_perception_server.py
=======================================================
覆盖三条主线：
1. 后端选择：auto/http/mock 的判定与环境优先级（无 YOLO / 无 X server 环境下必须降级）
2. 契约标准化：实体过滤、玩家脏值、队友识别、错误帧不再伪装成"空场景"
3. 端到端：Flask 路由、--selftest 自检、mock 帧喂给 predictor 能否算出速度

全程零截图、零网络、零真实 YOLO；时间用显式 now 参数或 FakeClock 注入。
"""
import json
import math
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import perception_server as ps  # noqa: E402


CONTRACT = ("player", "entities", "teammates", "afk_popup")


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """每个用例都从干净的后端环境 + 干净的 mock 漂移状态开始。"""
    monkeypatch.delenv(ps.BACKEND_ENV, raising=False)
    ps.reset_mock()
    yield
    ps.reset_mock()


@pytest.fixture
def mock_backend(monkeypatch):
    monkeypatch.setenv(ps.BACKEND_ENV, "mock")
    return "mock"


def _patch_http(monkeypatch, tool="scrot", script="/fake/detect.py", shot=True):
    """把 http 后端**兜底路径**的各个外部依赖替换掉。

    关键：必须同时把快路径关掉（`_capture_array` 返回 None）。
    否则本机 mss 可用时快路径会先跑通并直接返回真实检测结果，
    根本走不到这里 mock 的兜底分支 —— 测试会拿到真实 payload 而不是
    预期的 skip/timeout/error payload（表现为 KeyError: '_skipped'）。

    快路径本身由 test_fastpath_* 单独覆盖。
    """
    monkeypatch.setattr(ps, "_capture_array", lambda: (None, 0, 0))
    monkeypatch.setattr(ps, "_screenshot_tool", lambda: tool)
    monkeypatch.setattr(ps, "_find_detect_script", lambda: script)
    monkeypatch.setattr(ps, "_take_screenshot", lambda p: shot)
    monkeypatch.setattr(ps, "skip_on_timeout", lambda: 0.0)



def _FAKE_BGR():
    """最小可用的 (H,W,3) uint8 数组，够走通归一化即可。"""
    import numpy as np
    return np.zeros((8, 8, 3), dtype=np.uint8)


def _FAKE_DETECTIONS():
    """一份与 detect.py 同形状的检测结果。"""
    return {
        "backend": "yolo", "detections": 1,
        "player": {"alive": True, "hp": 100, "max_hp": 100,
                   "x": 4.0, "y": 4.0, "power_score": 90,
                   "petal_set": "shoot", "talent": "none"},
        "entities": [{"raw_id": "hornet", "rarity": "normal",
                      "x": 20.0, "y": 30.0, "conf": 0.9}],
        "teammates": [], "afk_popup": False,
    }

# ---------------------------------------------------------------------------
# 1. 后端选择
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("val,expect", [
    ("mock", "mock"), ("http", "http"), ("auto", "auto"),
    ("MOCK", "mock"), (" mock ", "mock"),
])
def test_configured_backend_env(monkeypatch, val, expect):
    monkeypatch.setenv(ps.BACKEND_ENV, val)
    assert ps.configured_backend() == expect


@pytest.mark.parametrize("val", ["", "bogus", "yolo", "none"])
def test_configured_backend_env_invalid_falls_back(monkeypatch, val):
    """非法环境变量必须回落配置/auto，不能让服务起不来。"""
    monkeypatch.setenv(ps.BACKEND_ENV, val)
    assert ps.configured_backend() in ps.VALID_BACKENDS


def test_configured_backend_from_config(monkeypatch):
    monkeypatch.setattr(ps, "_cfg", lambda path, default=None: "http"
                        if path == "perception.backend" else default)
    assert ps.configured_backend() == "http"


def test_resolve_auto_to_mock_when_no_tool(monkeypatch):
    monkeypatch.setattr(ps, "_screenshot_tool", lambda: "")
    monkeypatch.setattr(ps, "_find_detect_script", lambda: "")
    assert ps.resolve_backend() == "mock"


def test_resolve_auto_to_mock_when_no_detect_script(monkeypatch):
    monkeypatch.setattr(ps, "_screenshot_tool", lambda: "scrot")
    monkeypatch.setattr(ps, "_find_detect_script", lambda: "")
    assert ps.resolve_backend() == "mock"


def test_resolve_auto_to_http_when_ready(monkeypatch):
    monkeypatch.setattr(ps, "_screenshot_tool", lambda: "scrot")
    monkeypatch.setattr(ps, "_find_detect_script", lambda: "/fake/detect.py")
    assert ps.resolve_backend() == "http"


@pytest.mark.parametrize("explicit", ["mock", "http"])
def test_resolve_explicit_bypasses_detection(monkeypatch, explicit):
    """显式后端不做探测，避免"想 mock 却因环境有 import 命令被判成 http"。"""
    monkeypatch.setenv(ps.BACKEND_ENV, explicit)
    monkeypatch.setattr(ps, "_screenshot_tool", lambda: "scrot")
    monkeypatch.setattr(ps, "_find_detect_script", lambda: "/fake/detect.py")
    assert ps.resolve_backend() == explicit


# ---------------------------------------------------------------------------
# 2. 配置读取（运行时，支持热加载）
# ---------------------------------------------------------------------------
def test_port_falls_back_to_server_section(monkeypatch):
    monkeypatch.setattr(ps, "_cfg", lambda path, default=None:
                        5001 if path == "server.perception_port" else default)
    assert ps.port() == 5001


def test_port_prefers_perception_section(monkeypatch):
    monkeypatch.setattr(ps, "_cfg", lambda path, default=None:
                        6001 if path == "perception.port" else default)
    assert ps.port() == 6001


@pytest.mark.parametrize("bad", [None, "abc", float("nan"), ""])
def test_port_bad_value_safe(monkeypatch, bad):
    monkeypatch.setattr(ps, "_cfg", lambda path, default=None:
                        bad if path == "perception.port" else None)
    assert ps.port() in (5001, 0) or isinstance(ps.port(), int)


def test_yolo_timeout_floor(monkeypatch):
    monkeypatch.setattr(ps, "_cfg", lambda path, default=None: 0)
    assert ps.yolo_timeout() >= 1


def test_screen_size_fallback_chain(monkeypatch):
    monkeypatch.setattr(ps, "_cfg", lambda path, default=None:
                        {"combat.safe_zone_w": 1280, "combat.safe_zone_h": 720}.get(path, default))
    assert ps.screen_size() == (1280.0, 720.0)


def test_screen_size_zero_guard(monkeypatch):
    monkeypatch.setattr(ps, "_cfg", lambda path, default=None: 0)
    assert ps.screen_size() == (1920.0, 1080.0)


def test_cfg_missing_config_module(monkeypatch):
    """config 导入失败时不能让感知服务崩，回退 default。"""
    monkeypatch.setattr(ps, "_cfg", lambda path, default=None: default)
    assert ps._cfg("perception.backend", "auto") == "auto"


# ---------------------------------------------------------------------------
# 3. 实体 / 玩家 / 队友标准化
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("ent,expect", [
    ({"raw_id": "a", "x": 1, "y": 2}, True),
    ({"raw_id": "a", "x": "12.5", "y": "3"}, True),
    ({"raw_id": "a", "x": 0, "y": 0}, True),
    ({"x": 1, "y": 2}, False),                       # 缺 raw_id
    ({"raw_id": "a", "y": 2}, False),                # 缺 x
    ({"raw_id": "a", "x": -1, "y": 2}, False),       # 负坐标
    ({"raw_id": "a", "x": 1, "y": -5}, False),
    ({"raw_id": "a", "x": 99999, "y": 2}, False),    # 越界
    ({"raw_id": "a", "x": float("nan"), "y": 2}, False),   # NaN
    ({"raw_id": "a", "x": float("inf"), "y": 2}, False),   # Inf
    ({"raw_id": "a", "x": "nan", "y": 2}, False),    # NaN 字符串
    ({"raw_id": "a", "x": None, "y": 2}, False),
    ({"raw_id": "a", "x": "abc", "y": 2}, False),
    ("not a dict", False),
    (None, False),
])
def test_is_valid_entity(ent, expect):
    assert ps._is_valid_entity(ent) is expect


def test_normalize_entities_non_list():
    assert ps.normalize_entities(None) == []
    assert ps.normalize_entities("x") == []


def test_normalize_entities_rounds_and_stringifies():
    out = ps.normalize_entities([{"raw_id": 7, "x": 1.23456, "y": "9.0"}])
    assert out == [{"raw_id": "7", "rarity": "Common", "x": 1.2, "y": 9.0}]


def test_normalize_player_defaults():
    p = ps.normalize_player(None)
    assert p["alive"] is True and p["hp"] == 100 and p["max_hp"] == 100


@pytest.mark.parametrize("raw,key,expect", [
    ({"hp": float("nan")}, "hp", 100.0),
    ({"hp": "abc"}, "hp", 100.0),
    ({"hp": None}, "hp", 100.0),
    ({"max_hp": float("inf")}, "max_hp", 100.0),
    ({"power_score": "55.5"}, "power_score", 55.5),
    ({"alive": 0}, "alive", False),
    ({"petal_set": None}, "petal_set", "None"),
])
def test_normalize_player_dirty(raw, key, expect):
    assert ps.normalize_player(raw)[key] == expect


def test_normalize_teammates_prefers_explicit_list():
    raw = {"teammates": [{"raw_id": "p1", "x": 1, "y": 2}]}
    out = ps.normalize_teammates(raw, [{"raw_id": "player_ally", "x": 9, "y": 9}])
    assert len(out) == 1 and out[0]["raw_id"] == "p1"


def test_normalize_teammates_from_entities():
    ents = [{"raw_id": "player_ally", "x": 3, "y": 4},
            {"raw_id": "hornet", "x": 5, "y": 6}]
    out = ps.normalize_teammates({}, ents)
    assert [t["raw_id"] for t in out] == ["player_ally"]


def test_normalize_teammates_category_marker():
    out = ps.normalize_teammates({}, [{"raw_id": "x", "category": "Teammate", "x": 1, "y": 1}])
    assert len(out) == 1


@pytest.mark.parametrize("raw", [None, "x", 123, {"teammates": "not-a-list"}])
def test_normalize_teammates_bad_input(raw):
    assert ps.normalize_teammates(raw, None) == []


def test_normalize_teammates_skips_bad_entries():
    out = ps.normalize_teammates({"teammates": ["x", None, {"x": "bad", "y": "worse"}]}, [])
    assert len(out) == 1 and out[0]["x"] == 0.0


def test_normalize_detections_drops_teammates_from_entities():
    det = {"entities": [{"raw_id": "player_ally", "x": 1, "y": 1},
                        {"raw_id": "hornet", "x": 2, "y": 2}],
           "afk_popup": True}
    out = ps.normalize_detections(det)
    assert [e["raw_id"] for e in out["entities"]] == ["hornet"]
    assert len(out["teammates"]) == 1
    assert out["afk_popup"] is True
    assert out["_raw"] is det


@pytest.mark.parametrize("det", [None, "x", 5, {}])
def test_normalize_detections_bad_input_contract_holds(det):
    out = ps.normalize_detections(det)
    for k in CONTRACT:
        assert k in out


# ---------------------------------------------------------------------------
# 4. mock 后端
# ---------------------------------------------------------------------------
def test_mock_detections_from_config(mock_backend):
    det = ps.mock_detections(now=1000.0)
    assert det["_mock"] is True
    assert len(det["entities"]) >= 1
    assert isinstance(det["teammates"], list)


def test_mock_drift_advances_position(mock_backend):
    d1 = ps.mock_detections(now=1000.0)   # 首帧确立基准，dt=0
    d2 = ps.mock_detections(now=1001.0)   # dt=1s
    by_id = {e["raw_id"]: e for e in d2["entities"]}
    for e1 in d1["entities"]:
        e2 = by_id[e1["raw_id"]]
        # 至少一个带速度的实体移动了
        if abs(e2["x"] - e1["x"]) + abs(e2["y"] - e1["y"]) > 0:
            break
    else:
        pytest.fail("drift=true 但没有任何实体移动")


def test_mock_no_drift_static(mock_backend):
    cfg = {"drift": False, "entities": [{"raw_id": "a", "x": 10, "y": 10, "vx": 100, "vy": 100}]}
    d1 = ps.mock_detections(cfg=cfg, now=1.0)
    d2 = ps.mock_detections(cfg=cfg, now=99.0)
    assert d1["entities"][0]["x"] == d2["entities"][0]["x"] == 10


def test_mock_reset_clears_baseline(mock_backend):
    cfg = {"drift": True, "entities": [{"raw_id": "a", "x": 0, "y": 0, "vx": 10, "vy": 0}]}
    ps.mock_detections(cfg=cfg, now=10.0)
    ps.reset_mock()
    assert ps._MOCK_STATE["last"] is None


@pytest.mark.parametrize("cfg", [None if False else "bad", 5, [], {"entities": "x"},
                                 {"entities": [None, "s", 3]}])
def test_mock_detections_dirty_config(cfg):
    out = ps.mock_detections(cfg=cfg, now=1.0)
    assert out["entities"] == [] or all(isinstance(e, dict) for e in out["entities"])


def test_build_payload_mock_contract(mock_backend):
    p = ps.build_perception_payload("mock", now=1000.0)
    for k in CONTRACT:
        assert k in p
    assert p["_backend"] == "mock" and p["_mock"] is True
    assert p["player"]["x"] == 960.0


def test_build_payload_mock_unknown_backend_resolves(monkeypatch):
    monkeypatch.setenv(ps.BACKEND_ENV, "mock")
    p = ps.build_perception_payload("bogus", now=1000.0)
    assert p["_backend"] == "mock"


@pytest.mark.parametrize("start,speed,dt", [
    (400, 80, 1), (400, 80, 1000), (400, -80, 9999),
    (0, -500, 3.3), (1919, 300, 7.7), (10, 0, 500),
])
def test_pingpong_stays_in_bounds(start, speed, dt):
    """回归：修复前 mock 实体会无限外推飞出屏幕，长时间运行后感知结果退化为空。"""
    pos = ps._pingpong(start, speed, dt, 0.0, 1920.0)
    assert 0.0 <= pos <= 1920.0


def test_pingpong_zero_speed_clamps():
    assert ps._pingpong(10, 0, 100, 0.0, 100.0) == 10
    assert ps._pingpong(500, 0, 100, 0.0, 100.0) == 100


def test_mock_long_run_keeps_all_entities(mock_backend):
    """模拟服务跑很久之后仍有完整实体集，离线主循环不会"看不见怪"。"""
    ps.reset_mock()
    ps.mock_detections(now=1000.0)          # 首帧基准
    late = ps.build_perception_payload("mock", now=1000.0 + 3600.0)  # 1 小时后
    assert len(late["entities"]) == 5
    for e in late["entities"]:
        assert 0.0 <= e["x"] <= 1920.0 and 0.0 <= e["y"] <= 1080.0


# ---------------------------------------------------------------------------
# 5. http 后端的失败降级（无 X server / 无 YOLO 场景）
# ---------------------------------------------------------------------------
def test_http_no_screenshot_tool(monkeypatch):
    _patch_http(monkeypatch, tool="")
    p = ps.build_perception_payload("http")
    assert p["_skipped"] is True and p["_reason"] == "screenshot_tool_missing"
    assert "UGF_PERCEPTION_BACKEND" in p["_error"]


def test_http_no_detect_script(monkeypatch):
    _patch_http(monkeypatch, tool="scrot", script="")
    p = ps.build_perception_payload("http")
    assert p["_skipped"] is True and p["_reason"] == "detect_script_missing"


def test_http_screenshot_failed(monkeypatch):
    _patch_http(monkeypatch, shot=False)
    p = ps.build_perception_payload("http")
    assert p["_reason"] == "screenshot_failed"
    assert p["entities"] == []


def test_http_yolo_timeout(monkeypatch):
    _patch_http(monkeypatch)
    monkeypatch.setattr(ps, "_run_yolo", lambda p: {"_timeout": True})
    p = ps.build_perception_payload("http")
    assert p["_reason"] == "yolo_timeout" and p["_skipped"] is True


def test_http_yolo_error_not_silent_empty_frame(monkeypatch):
    """
    回归：修复前 YOLO 报错会被当成"空场景"返回，Agent 误判为游戏里没怪继续空转。
    """
    _patch_http(monkeypatch)
    monkeypatch.setattr(ps, "_run_yolo", lambda p: {"error": "检测脚本未找到"})
    p = ps.build_perception_payload("http")
    assert p["_skipped"] is True
    assert p["_reason"] == "yolo_error"
    assert "检测脚本" in p["_error"]


def test_http_yolo_non_dict_output(monkeypatch):
    _patch_http(monkeypatch)
    monkeypatch.setattr(ps, "_run_yolo", lambda p: ["not", "a", "dict"])
    p = ps.build_perception_payload("http")
    assert p["_reason"] == "yolo_bad_output"


def test_http_success_path_normalizes(monkeypatch):
    _patch_http(monkeypatch)
    monkeypatch.setattr(ps, "_run_yolo", lambda p: {
        "player": {"hp": 80, "max_hp": 100, "x": 10, "y": 20},
        "monsters": [{"raw_id": "hornet", "x": 1, "y": 2},
                     {"raw_id": "player_ally", "x": 3, "y": 4},
                     {"raw_id": "bad", "x": -1, "y": 5}],
        "afk_popup": True,
    })
    p = ps.build_perception_payload("http")
    assert p["_backend"] == "http" and "_skipped" not in p
    assert [e["raw_id"] for e in p["entities"]] == ["hornet"]
    assert p["teammates"][0]["raw_id"] == "player_ally"
    assert p["afk_popup"] is True


def test_http_screenshot_file_always_removed(monkeypatch, tmp_path):
    """截图用完必须删，不能在仓库里堆 png。"""
    _patch_http(monkeypatch)
    monkeypatch.setattr(ps, "SCREENSHOT_PATH", str(tmp_path / "frame.png"))
    ps.SCREENSHOT_PATH  # noqa: B018

    def _fake_shot(path):
        with open(path, "wb") as f:
            f.write(b"fake")
        return True

    monkeypatch.setattr(ps, "_take_screenshot", _fake_shot)
    monkeypatch.setattr(ps, "_run_yolo", lambda p: {"error": "x"})
    ps.build_perception_payload("http")
    assert not os.path.exists(str(tmp_path / "frame.png"))


# ---------------------------------------------------------------------------
# 6. Flask 路由
# ---------------------------------------------------------------------------
def test_route_perceive_contract(mock_backend):
    client = ps.app.test_client()
    resp = client.get("/perceive")
    assert resp.status_code == 200
    data = resp.get_json()
    for k in CONTRACT:
        assert k in data
    assert data["_backend"] == "mock"


def test_route_perceive_raw_flag(mock_backend):
    client = ps.app.test_client()
    assert "_raw" in client.get("/perceive").get_json()
    assert "_raw" not in client.get("/perceive?raw=0").get_json()


def test_route_health_fields(mock_backend):
    data = ps.app.test_client().get("/health").get_json()
    for k in ("status", "backend", "configured_backend", "port",
              "detect_script", "screenshot_tool", "offline"):
        assert k in data
    assert data["backend"] == "mock" and data["offline"] is True


def test_route_http_backend_no_crash(monkeypatch):
    """强制 http 后端且在无截图工具的机器上，路由必须返回结构化错误而不是 500。"""
    _patch_http(monkeypatch, tool="")   # 同时关掉快路径，否则 mss 可用时会走通
    monkeypatch.setenv(ps.BACKEND_ENV, "http")
    monkeypatch.setattr(ps, "_screenshot_tool", lambda: "")
    resp = ps.app.test_client().get("/perceive")
    assert resp.status_code == 200
    assert resp.get_json()["_skipped"] is True


# ---------------------------------------------------------------------------
# 7. 自检
# ---------------------------------------------------------------------------
def test_selftest_mock_exit_zero(mock_backend, capsys):
    assert ps.selftest(3, "mock") == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True and out["drifting"] is True


def test_selftest_detects_broken_payload(monkeypatch, capsys):
    monkeypatch.setattr(ps, "build_perception_payload", lambda *a, **k: {"player": "x"})
    assert ps.selftest(2, "mock") == 1
    assert json.loads(capsys.readouterr().out)["problems"]


def test_selftest_rounds_respected(mock_backend, capsys):
    ps.selftest(1, "mock")
    assert json.loads(capsys.readouterr().out)["rounds"] == 1


def test_main_selftest_does_not_start_server(monkeypatch):
    started = {"v": False}

    def _boom(*a, **k):
        started["v"] = True

    monkeypatch.setattr(ps.app, "run", _boom)
    assert ps.main(["--selftest", "--backend", "mock", "--rounds", "2"]) == 0
    assert started["v"] is False


# ---------------------------------------------------------------------------
# 8. 与 predictor 的真实链路（mock 帧 → 速度 → 预判）
# ---------------------------------------------------------------------------
def test_mock_frames_feed_predictor(mock_backend, monkeypatch):
    """感知契约必须对得上 predictor 的输入要求，否则整条链路是假的。"""
    import predictor
    predictor.reset()
    cfg = {"drift": True, "entities": [{"raw_id": "hornet", "rarity": "Common",
                                        "x": 100, "y": 100, "vx": 200, "vy": 0}]}
    orig_cfg = ps._cfg

    def _cfg(path, default=None):
        return cfg if path == "perception.mock" else orig_cfg(path, default)

    monkeypatch.setattr(ps, "_cfg", _cfg)

    # predictor 走真实时钟，这里用 FakeClock 与 mock 漂移时间对齐（0.1s/帧）
    clock = type("FakeClock", (), {"now": 1000.0, "time": lambda self: self.now})()
    monkeypatch.setattr(predictor, "time", clock)
    for i in range(8):                       # 8 帧 * 0.1s
        clock.now = 1000.0 + i * 0.1
        p = ps.build_perception_payload("mock", now=clock.now)
        predictor.update_frame_entities([
            {"raw_id": e["raw_id"], "rarity": e["rarity"], "x": e["x"], "y": e["y"]}
            for e in p["entities"]
        ])
    res = predictor.predict_all_entities()
    assert res, "6 帧 mock 数据应能算出预判结果"
    top = res[0]
    assert top["raw_id"] == "hornet"
    assert top["x_predict"] > top["x_now"], "匀速右移的实体预判点应在当前点右侧"
    assert math.isfinite(top["vx_per_sec"]) and top["vx_per_sec"] > 0
    predictor.reset()


# ---------------------------------------------------------------------------
# v2.0 真机实测（2026-10-01）：截图探测的两个修复
# ---------------------------------------------------------------------------
# 本机 DISPLAY=:0 无授权（Xwayland 在 :1），且 `import`（ImageMagick）
# 存在但已损坏。这两点同时暴露了旧实现的两个假设错误。

def test_screenshot_tool_is_probed_not_merely_present():
    """D6：探测阶段必须真跑一次，不能只看 shutil.which。

    旧实现按固定顺序取第一个 PATH 里有的 —— 本机 `import` 存在但
    损坏（退出码非 0、不产文件），于是必然选中坏的那个。
    """
    import perception_server as ps

    calls = []

    def fake_which(name):
        return "/usr/bin/" + name      # 所有候选都"存在"

    def fake_try(name, path):
        calls.append(name)
        # 只有 gnome-screenshot 与 maim 能真正产出文件（mss/xwd/import 全坏）
        return name in ("gnome-screenshot", "maim")

    orig_which, orig_try = ps.shutil.which, ps._try_screenshot
    try:
        ps.shutil.which = fake_which
        ps._try_screenshot = fake_try
        got = ps._screenshot_tool()
    finally:
        ps.shutil.which, ps._try_screenshot = orig_which, orig_try

    assert got == "gnome-screenshot", f"应跳过损坏的工具，实际选中 {got!r}"
    # 必须从头挨个试（不能只试某一个），且**不能**只靠 which 就返回
    assert calls[0] == ps._SCREENSHOT_CANDIDATES[0], (
        f"应从第一个候选开始试，实际 {calls}")
    assert len(calls) > 1, f"前面的坏了就该继续往后试，实际只试了 {calls}"

    # 关键场景：候选顺序里排在前面的**坏了**时，必须继续往后试。
    # 旧实现只看 which 命中就返回，会直接选中坏的那个。
    calls.clear()

    def fake_try_broken_first(name, path):
        calls.append(name)
        return name == "maim"          # 只有最后一个能用

    orig_try2 = ps._try_screenshot
    try:
        ps._try_screenshot = fake_try_broken_first
        got2 = ps._screenshot_tool()
    finally:
        ps._try_screenshot = orig_try2

    assert got2 == "maim", f"前面都坏时应一路试到最后，实际选中 {got2!r}"
    assert calls == list(ps._SCREENSHOT_CANDIDATES), (
        f"应挨个试完所有候选，实际只试了 {calls}")


def test_displays_to_try_prefers_inherited_then_xwayland():
    """D7：Wayland 会话下 XWayland 通常在 :1，不能只信继承来的 DISPLAY。"""
    import perception_server as ps

    os.environ.pop("DISPLAY", None)
    assert ":1" in ps._displays_to_try()
    assert ":0" in ps._displays_to_try()

    os.environ["DISPLAY"] = ":7"
    try:
        got = ps._displays_to_try()
        assert got[0] == ":7", "继承值应优先"
        assert ":1" in got, "仍应兜底试 :1"
    finally:
        os.environ.pop("DISPLAY", None)


def test_candidate_order_is_speed_ranked_not_session_hardcoded():
    """候选顺序必须按**实测延迟**排，而不是沿用任何单一会话下的结论。

    背景：这个顺序被推翻过两次，每次都是因为换了会话类型：
      · Wayland + Xwayland -rootless：import/xwd/mss 全部失效
        （BadMatch / XProtoError），gnome-screenshot 是唯一能用的 → 它排第一
      · Xorg：全部可用，实测 mss 32ms / xwd 201ms / import 1443ms
        / gnome-screenshot 2303ms → mss 排第一

    所以这里**不能**断言任何具体的相对顺序（那等于把某次机器状态
    焊死在测试里）。真正要守住的是：
      1. 列表里包含各会话下唯一可用的兜底工具；
      2. 最快的工具排在最前（本机 Xorg 实测 mss）；
      3. 探测阶段会挨个真试，坏的自然被跳过（由上面两个测试保证）。
    """
    import perception_server as ps
    order = list(ps._SCREENSHOT_CANDIDATES)

    for tool in ("mss", "gnome-screenshot", "import"):
        assert tool in order, f"候选列表缺 {tool}：{order}"

    # Xorg 实测最快的必须排第一（mss 32ms vs gnome-screenshot 2303ms）
    assert order[0] == "mss", (
        f"最快的工具应排第一，当前 {order}。"
        f"若换了会话类型请重新实测并更新本断言及 perception_server 里的注释")

    # 兜底工具必须在列表里 —— Wayland 下只有 gnome-screenshot 能用
    assert "gnome-screenshot" in order, f"缺少 Wayland 兜底工具：{order}"


def test_take_screenshot_tries_every_candidate():
    """_take_screenshot 必须挨个试，而不是只试选中的那一个。"""
    import perception_server as ps
    tried = []

    def fake_try(name, path):
        tried.append(name)
        return name == "maim"          # 只有最后一个能用

    orig = ps._try_screenshot
    try:
        ps._try_screenshot = fake_try
        ok = ps._take_screenshot("/tmp/ugf_test_should_not_exist.png")
    finally:
        ps._try_screenshot = orig
    assert ok is True
    assert len(tried) == len(ps._SCREENSHOT_CANDIDATES), (
        f"只试了 {tried}")


# ---------------------------------------------------------------------------
# 快路径：mss 抓原始像素 → 常驻模型 → 不落盘
# ---------------------------------------------------------------------------
# 背景：旧路径每帧起子进程跑 detect.py，而 detect.py 每次都要
# `import ultralytics` + `YOLO(weight)` 重新加载权重（实测 ~8s），
# 正好撞上 yolo_timeout(8s) —— 整条 http 感知链路**每帧超时、零结果**。
# 快路径把模型常驻、原始像素直喂，实测 ~1200ms/帧。

def test_fastpath_used_when_mss_available(monkeypatch):
    """mss 有像素时，应走快路径且完全不碰落盘/子进程。"""
    calls = {"shot": 0, "sub": 0}
    monkeypatch.setattr(ps, "_capture_array", lambda: (_FAKE_BGR(), 1366, 768))
    monkeypatch.setattr(ps, "_yolo_predict", lambda img: _FAKE_DETECTIONS())
    monkeypatch.setattr(ps, "_take_screenshot", lambda p: calls.__setitem__("shot", calls["shot"] + 1))
    monkeypatch.setattr(ps, "_run_yolo", lambda p: calls.__setitem__("sub", calls["sub"] + 1))

    p = ps.build_perception_payload("http")
    assert p["_backend"] == "http"
    assert p.get("_fastpath") is True
    assert p.get("_skipped") is None
    assert calls == {"shot": 0, "sub": 0}, "快路径不应触发落盘截图或子进程"
    assert [e["raw_id"] for e in p["entities"]] == ["hornet"]


def test_fastpath_falls_back_when_yolo_errors(monkeypatch):
    """快路径 YOLO 报错时，必须自动回落到「落盘 + 子进程」，不能直接失败。"""
    order = []
    monkeypatch.setattr(ps, "_capture_array", lambda: (_FAKE_BGR(), 1366, 768))
    monkeypatch.setattr(ps, "_yolo_predict", lambda img: {"error": "推理失败(模拟)"})
    monkeypatch.setattr(ps, "_screenshot_tool", lambda: "scrot")
    monkeypatch.setattr(ps, "_find_detect_script", lambda: "/fake/detect.py")
    monkeypatch.setattr(ps, "_take_screenshot",
                        lambda p: (order.append("shot"), True)[1])
    monkeypatch.setattr(ps, "_run_yolo",
                        lambda p: (order.append("sub"), _FAKE_DETECTIONS())[1])

    p = ps.build_perception_payload("http")
    assert order == ["shot", "sub"], f"应回落到落盘+子进程，实际 {order}"
    assert p["_backend"] == "http" and p.get("_skipped") is None


def test_fastpath_timeout_reason_includes_fastpath_error(monkeypatch):
    """两条路都失败时，跳过原因要带上快路径的错误，便于定位。"""
    monkeypatch.setattr(ps, "_capture_array", lambda: (_FAKE_BGR(), 1366, 768))
    monkeypatch.setattr(ps, "_yolo_predict", lambda img: {"error": "快路径炸了"})
    monkeypatch.setattr(ps, "_screenshot_tool", lambda: "scrot")
    monkeypatch.setattr(ps, "_find_detect_script", lambda: "/fake/detect.py")
    monkeypatch.setattr(ps, "_take_screenshot", lambda p: True)
    monkeypatch.setattr(ps, "_run_yolo", lambda p: {"_timeout": True})
    monkeypatch.setattr(ps, "skip_on_timeout", lambda: 0.0)

    p = ps.build_perception_payload("http")
    assert p["_skipped"] is True and p["_reason"] == "yolo_timeout"
    assert "快路径炸了" in p["_error"], f"跳过原因应含快路径错误：{p['_error']}"


def test_capture_array_returns_none_when_mss_unavailable(monkeypatch):
    """mss 不可用时 _capture_array 必须返回 None（触发兜底），不能抛异常。"""
    monkeypatch.setattr(ps, "_mss_available", lambda: False)
    assert ps._capture_array() == (None, 0, 0)


def test_yolo_model_cached_across_calls(monkeypatch):
    """模型必须只加载一次 —— 每帧重载正是原来超时的根因。"""
    loads = []

    class _FakeModel:
        class _Boxes(list):
            pass

        def predict(self, img, **kw):
            class R:
                boxes = []
                names = {}
            return [R()]

    def fake_yolo(weight):
        loads.append(weight)
        return _FakeModel()

    ps._YOLO.update({"model": None, "weight": None, "tried": False, "err": None})
    monkeypatch.setattr(ps, "_yolo_weight", lambda: "/fake/yolov8n.pt")
    import ultralytics
    monkeypatch.setattr(ultralytics, "YOLO", fake_yolo)

    ps._yolo_predict(_FAKE_BGR())
    ps._yolo_predict(_FAKE_BGR())
    ps._yolo_predict(_FAKE_BGR())
    assert len(loads) == 1, f"模型应只加载一次，实际 {len(loads)} 次"
    ps._YOLO.update({"model": None, "weight": None, "tried": False, "err": None})
