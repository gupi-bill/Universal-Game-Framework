# ===========================================================================
# 6. 感知（mock 离线合成 / http 接外部检测服务）
# ===========================================================================
PERCEPTION_BACKENDS = ("mock", "http", "local", "template")


def _player_stub() -> dict:
    """local/template 后端的玩家状态桩（ROADMAP #6）。

    这两类后端只做实体检测、不解析玩家面板；可在档案里用
    perception.player_stub 显式覆盖（比如接入血量 OCR 前先保守给值）。
    """
    stub = cfg_get("perception.player_stub", None)
    if isinstance(stub, dict) and stub:
        return dict(stub)
    sw = safe_float(cfg_get("combat.safe_zone_w", 1920), 1920)
    sh = safe_float(cfg_get("combat.safe_zone_h", 1080), 1080)
    return {
        "alive": True,
        "hp": 100,
        "max_hp": 100,
        "x": sw / 2,
        "y": sh / 2,
        "power_score": 100,
        "petal_set": str(cfg_get("combat.default_set", "combat")),
        "talent": "none",
    }


def _capture_region():
    """capture_region 双格式（ROADMAP v2 #8）：dict 原样，"x,y,w,h" 字符串简写解析。

    非法格式留日志并回落 None（全屏）。
    """
    raw = cfg_get("perception.capture_region", None)
    if isinstance(raw, dict) and raw:
        return raw
    if isinstance(raw, str) and raw.strip():
        parts = [p.strip() for p in raw.split(",")]
        if len(parts) == 4:
            try:
                v = [int(float(p)) for p in parts]
                return {"left": v[0], "top": v[1], "width": v[2], "height": v[3]}
            except ValueError:
                pass
        log(f'[感知] capture_region 格式非法（{raw!r}），回落全屏；应为 "x,y,w,h" 或 dict')
    return None


def _grab_screen():
    """mss 抓屏 → BGR ndarray。返回 (img, err)；依赖缺失给明确安装指引。"""
    try:
        import mss
        import numpy as np
    except ImportError as e:
        return None, f"抓屏需要 mss+numpy（pip install mss numpy）: {e}"
    try:
        region = _capture_region()
        with mss.mss() as sct:
            mon = region if region else sct.monitors[1]
            shot = sct.grab(mon)
            img = np.asarray(shot, dtype="uint8")[:, :, :3].copy()  # BGRA -> BGR
        return img, ""
    except Exception as e:  # 无显示环境 / 区域非法等
        return None, f"抓屏失败: {type(e).__name__}: {e}"


def _iou_c(ax, ay, aw, ah, bx, by, bw, bh) -> float:
    """中心点+宽高 表示的两框 IoU。"""
    ax1, ay1, ax2, ay2 = ax - aw / 2, ay - ah / 2, ax + aw / 2, ay + ah / 2
    bx1, by1, bx2, by2 = bx - bw / 2, by - bh / 2, bx + bw / 2, by + bh / 2
    ix = max(0.0, min(ax2, bx2) - max(ax1, bx1))
    iy = max(0.0, min(ay2, by2) - max(ay1, by1))
    inter = ix * iy
    union = aw * ah + bw * bh - inter
    return inter / union if union > 0 else 0.0


def _parse_yolo_output(
    out, src_w: int, src_h: int, size: int, conf_th: float, labels: list, rarity_map: dict
) -> list:
    """解析 YOLOv8 风格检测输出 [1, 4+nc, anchors]（或转置形态）→ 实体列表。

    blobFromImage 整图 resize（不保长宽比），坐标按 size→原图等比映回；
    贪心 NMS（IoU 0.4）。需要 numpy；纯函数，便于离线单测。
    """
    import numpy as np

    arr = np.asarray(out[0] if isinstance(out, (list, tuple)) else out, dtype="float32")
    if arr.ndim == 3:
        arr = arr[0]
    if arr.ndim != 2:
        return []

    def _valid(a) -> bool:
        return a.ndim == 2 and a.shape[0] - 4 >= 1 and a.shape[1] >= 1

    # 定向判定：标准形态 [4+nc, anchors] 且特征维≤锚点维时保持原样；
    # 否则若转置形态有效则转置；两向都无效才拒绝（容忍锚点极少的退化形态）
    if not (_valid(arr) and arr.shape[0] <= arr.shape[1]):
        if _valid(arr.T) and (arr.T.shape[0] <= arr.T.shape[1] or not _valid(arr)):
            arr = arr.T
        elif not _valid(arr):
            return []
    nc = arr.shape[0] - 4
    if nc <= 0 or arr.shape[1] == 0:
        return []
    cls_scores = arr[4:, :]
    conf = cls_scores.max(axis=0)
    cls = cls_scores.argmax(axis=0)
    mask = conf >= conf_th
    if not mask.any():
        return []
    sx, sy = src_w / float(size), src_h / float(size)
    boxes: list = []
    for i in np.flatnonzero(mask):
        cx, cy, w, h = (float(v) for v in arr[:4, i])
        idx = int(cls[i])
        label = str(labels[idx]) if idx < len(labels) else f"class_{idx}"
        boxes.append(
            {
                "cx": cx * sx,
                "cy": cy * sy,
                "w": w * sx,
                "h": h * sy,
                "conf": float(conf[i]),
                "label": label,
                "rarity": str((rarity_map or {}).get(label, "Common")),
            }
        )
    boxes.sort(key=lambda b: -b["conf"])
    kept: list = []
    for b in boxes:
        if all(
            _iou_c(b["cx"], b["cy"], b["w"], b["h"], k["cx"], k["cy"], k["w"], k["h"]) < 0.4 for k in kept
        ):
            kept.append(b)
    return [
        {"raw_id": b["label"], "rarity": b["rarity"], "x": round(b["cx"], 1), "y": round(b["cy"], 1)}
        for b in kept[:64]
    ]


class Perception:
    """返回统一 payload: {player, entities, teammates, afk_popup, _fallback?}

    后端（ROADMAP #6 插件化）：
    - mock     离线合成（档案 perception.mock）
    - http     接外部检测服务（原 perception_server）
    - local    mss 抓屏 + ONNX Runtime 本地推理（YOLOv8 导出的 .onnx）
    - template OpenCV 模板匹配（每类实体一张 <label>.png，零模型）
    """

    _ort_cache: dict = {}

    def __init__(self):
        self._mock = self._load_mock()

    def _load_mock(self) -> dict:
        prof = _load_profile_chain(safe_name(active_game()))
        m = (prof.get("perception") or {}).get("mock") or {}
        if not m:
            m = {
                "drift": True,
                "afk_popup": False,
                "player": {
                    "alive": True,
                    "hp": 100,
                    "max_hp": 100,
                    "x": 960,
                    "y": 540,
                    "power_score": 120,
                    "petal_set": "combat",
                    "talent": "none",
                },
                "entities": [
                    {"raw_id": "hornet", "rarity": "Common", "x": 400, "y": 300, "vx": 80, "vy": 0},
                    {"raw_id": "beetle", "rarity": "Epic", "x": 1500, "y": 350, "vx": -30, "vy": -30},
                    {"raw_id": "mantis", "rarity": "Super", "x": 1700, "y": 800, "vx": 0, "vy": 0},
                ],
                "teammates": [],
            }
        self._state = [dict(e) for e in (m.get("entities") or [])]
        self._t0 = time.time()
        return m

    def backend(self) -> str:
        b = (os.getenv("UGF_PERCEPTION_BACKEND") or "").strip().lower() or str(
            cfg_get("perception.backend", "auto")
        ).lower()
        if b == "auto":
            return "mock" if dry_run() else "http"
        if b not in PERCEPTION_BACKENDS:
            log(f"[感知] 未知后端 {b!r}（可选 {'/'.join(PERCEPTION_BACKENDS)}），回落 mock")
            return "mock"
        return b

    def _mock_frame(self) -> FramePayload:
        drift = bool(self._mock.get("drift", True))
        dt = 1.0 / 30.0
        ents = []
        sw, sh = 1920, 1080
        for e in self._state:
            if drift:
                e["x"] = max(10, min(sw - 10, safe_float(e.get("x")) + safe_float(e.get("vx")) * dt))
                e["y"] = max(10, min(sh - 10, safe_float(e.get("y")) + safe_float(e.get("vy")) * dt))
            ents.append(
                {
                    "raw_id": e.get("raw_id"),
                    "rarity": e.get("rarity"),
                    "x": round(safe_float(e.get("x")), 1),
                    "y": round(safe_float(e.get("y")), 1),
                }
            )
        player = dict(self._mock.get("player") or {})
        # 玩家缓慢绕圈，方便观察走位
        ang = (time.time() - self._t0) * 0.6
        player["x"] = round(960 + 220 * math.cos(ang), 1)
        player["y"] = round(540 + 140 * math.sin(ang), 1)
        return {
            "player": player,
            "entities": ents,
            "teammates": [dict(t) for t in (self._mock.get("teammates") or [])],
            "afk_popup": bool(self._mock.get("afk_popup", False)),
            "_fallback": "mock",
        }

    def _http_frame(self) -> FramePayload:
        try:
            import requests
        except ImportError:
            return {"error": "未安装 requests，无法走 http 感知后端（可设 UGF_DRY_RUN=1 用 mock）"}
        url = (os.getenv("UGF_PERCEPTION_URL") or "").strip() or cfg_get("perception.http_url", "")
        if not url:
            url = f"http://127.0.0.1:{safe_int(cfg_get('server.perception_port', 5001), 5001)}/perceive"
        attempts = safe_int(cfg_get("resilience.perception.retries", 2), 2) + 1
        backoff = safe_float(cfg_get("resilience.perception.backoff", 1.0), 1.0)

        def _fetch():
            r = requests.get(url, timeout=safe_float(cfg_get("perception.timeout", 24), 24))
            r.raise_for_status()
            data = r.json()
            data.pop("_raw", None)
            if not isinstance(data, dict):
                raise ValueError("感知服务返回非 JSON 对象")
            return data

        ok, data, err = retry_call(_fetch, attempts, backoff, "感知服务")
        if ok:
            return data
        return {"error": f"感知服务不可用: {err}"}

    def _local_session(self, model_path: str, ort):
        """ONNX 会话按 (路径, mtime) 缓存，换模型文件自动重建。"""
        key = (model_path, os.path.getmtime(model_path))
        sess = Perception._ort_cache.get("sess")
        if sess is None or Perception._ort_cache.get("key") != key:
            sess = ort.InferenceSession(model_path, providers=["CPUExecutionProvider"])
            Perception._ort_cache = {"key": key, "sess": sess}
        return sess

    def _local_frame(self) -> FramePayload:
        """local 后端：mss 抓屏 + ONNX Runtime 本地推理（YOLOv8 导出格式）。"""
        model_path = str(cfg_get("perception.local.model_path", "") or "").strip()
        if not model_path:
            return {"error": "local 后端未配置 perception.local.model_path（.onnx 模型路径）"}
        if not os.path.exists(model_path):
            return {"error": f"ONNX 模型不存在: {model_path}"}
        try:
            import onnxruntime as ort
        except ImportError as e:
            return {"error": f"local 后端需要 onnxruntime（pip install onnxruntime numpy）: {e}"}
        img, err = _grab_screen()
        if img is None:
            return {"error": err}
        try:
            import cv2

            size = safe_int(cfg_get("perception.local.input_size", 640), 640)
            blob = cv2.dnn.blobFromImage(img, 1 / 255.0, (size, size), swapRB=True, crop=False)
        except ImportError as e:
            return {"error": f"local 后端需要 opencv 做预处理（pip install opencv-python-headless）: {e}"}
        try:
            sess = self._local_session(model_path, ort)
            out = sess.run(None, {sess.get_inputs()[0].name: blob})
        except Exception as e:
            return {"error": f"ONNX 推理失败: {type(e).__name__}: {e}"}
        ents = _parse_yolo_output(
            out,
            img.shape[1],
            img.shape[0],
            size,
            safe_float(cfg_get("perception.local.conf", 0.5), 0.5),
            cfg_get("perception.local.labels", []) or [],
            cfg_get("perception.local.rarity_map", {}) or {},
        )
        return {
            "player": _player_stub(),
            "entities": ents,
            "teammates": [],
            "afk_popup": False,
            "_fallback": "local",
        }

    def _template_frame(self) -> FramePayload:
        """template 后端：OpenCV 模板匹配。模板目录里每个 <label>.png 一类实体。

        调试可用 perception.template.source_image 指定截图文件代替实时抓屏。
        """
        tdir = str(cfg_get("perception.template.dir", "") or "").strip()
        if not tdir or not os.path.isdir(tdir):
            return {
                "error": f"template 后端需配置 perception.template.dir 且目录存在（当前: {tdir or '空'}）"
            }
        try:
            import cv2
            import numpy as np
        except ImportError as e:
            return {
                "error": f"template 后端需要 opencv+numpy（pip install opencv-python-headless numpy）: {e}"
            }
        src_img = str(cfg_get("perception.template.source_image", "") or "").strip()
        if src_img and os.path.exists(src_img):
            img = cv2.imread(src_img)
            if img is None:
                return {"error": f"无法读取调试截图: {src_img}"}
        else:
            img, err = _grab_screen()
            if img is None:
                return {"error": err}
        th = safe_float(cfg_get("perception.template.conf", 0.8), 0.8)
        rarity_map = cfg_get("perception.template.rarity_map", {}) or {}
        ents = []
        for fn in sorted(os.listdir(tdir)):
            if not fn.lower().endswith(".png"):
                continue
            label = os.path.splitext(fn)[0]
            tpl = cv2.imread(os.path.join(tdir, fn))
            if tpl is None or tpl.shape[0] > img.shape[0] or tpl.shape[1] > img.shape[1]:
                continue
            res = cv2.matchTemplate(img, tpl, cv2.TM_CCOEFF_NORMED)
            locs = np.argwhere(res >= th)
            kept: list = []
            for ty, tx in locs:
                cx, cy = float(tx) + tpl.shape[1] / 2, float(ty) + tpl.shape[0] / 2
                w, h = float(tpl.shape[1]), float(tpl.shape[0])
                if all(_iou_c(cx, cy, w, h, k[0], k[1], k[2], k[3]) < 0.3 for k in kept):
                    kept.append((cx, cy, w, h))
            for cx, cy, _w, _h in kept[:32]:
                ents.append(
                    {
                        "raw_id": label,
                        "rarity": str(rarity_map.get(label, "Common")),
                        "x": round(cx, 1),
                        "y": round(cy, 1),
                    }
                )
        return {
            "player": _player_stub(),
            "entities": ents,
            "teammates": [],
            "afk_popup": False,
            "_fallback": "template",
        }

    def _vlm_frame(self) -> FramePayload:
        """vlm 后端：抓屏 → VLM API 识别 → 结构化 JSON。无需模型权重。"""
        url = (os.getenv("VLM_API_URL") or "").strip()
        key = (os.getenv("VLM_API_KEY") or "").strip()
        if not url or not key:
            return {"error": "vlm 后端需配置 VLM_API_URL / VLM_API_KEY"}
        img, err = _grab_screen()
        if img is None:
            return {"error": err}
        try:
            import base64
            import cv2
            import requests

            img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
            h, w = img.shape[:2]
            scale = min(1.0, 960.0 / max(w, 1))
            if scale < 1.0:
                img = cv2.resize(img, (int(w * scale), int(h * scale)))
            ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 65])
            if not ok:
                return {"error": "截图编码失败"}
            b64 = base64.b64encode(buf.tobytes()).decode()
            prompt = (
                "你是游戏视觉识别器。看这张游戏截图，只输出JSON：\n"
                '{"player":{"alive":true,"hp":100,"max_hp":100,"x":960,"y":540,'
                '"power_score":100},"entities":[{"raw_id":"怪物名","rarity":"Common|Unusual|Rare|'
                'Epic|Legendary|Mythic|Ultra|Super|Unique|Eternal","x":0,"y":0}],'
                '"afk_popup":false}\n'
                "坐标按屏幕像素。最多15个实体。加载/菜单界面则 player.alive=false。只输出JSON。"
            )
            r = requests.post(
                url,
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json={
                    "model": os.getenv("VLM_MODEL", ""),
                    "messages": [{"role": "user", "content": [
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                        {"type": "text", "text": prompt},
                    ]}],
                    "max_tokens": 500,
                    "temperature": 0.1,
                },
                timeout=25,
            )
            r.raise_for_status()
            content = r.json()["choices"][0]["message"]["content"].strip()
            if "```" in content:
                content = content.split("```")[1].split("```")[0]
                if content.startswith("json"):
                    content = content[4:]
            data = json.loads(content)
            data.setdefault("teammates", [])
            data.setdefault("afk_popup", False)
            data.setdefault("_fallback", "vlm")
            return data
        except Exception as e:
            return {"error": f"VLM感知失败: {type(e).__name__}: {e}"}

    def frame(self) -> FramePayload:
        b = self.backend()
        if b == "http":
            return self._http_frame()
        if b == "vlm":
            return self._vlm_frame()
        if b == "local":
            return self._local_frame()
        if b == "template":
            return self._template_frame()
        return self._mock_frame()

    def perceive(self) -> FramePayload:
        """取一帧并自动喂给预判模块（= 原 perceive_game）。"""
        data = self.frame()
        if "error" not in data:
            PREDICTOR.update(data.get("entities") or [])
        return data


