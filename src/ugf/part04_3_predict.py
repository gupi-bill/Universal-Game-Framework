# ===========================================================================
# 3. 预判（全实体运动预判 + 置信度）
# ===========================================================================
def _threat_table() -> dict:
    return dict(cfg_get("predictor.threat", DEFAULT["predictor.threat"]) or {})


def classify_by_rarity(rarity: str) -> str:
    r = (rarity or "").strip().capitalize()
    if r in set(cfg_get("predictor.rarity_highest_boss", [])):
        return "highest_boss"
    if r in set(cfg_get("predictor.rarity_boss", [])):
        return "boss"
    if r in set(cfg_get("predictor.rarity_elite", [])):
        return "elite"
    if r in set(cfg_get("predictor.rarity_normal", [])):
        return "normal"
    return "unknown"


PLAYER_ENEMY_MARKERS = ("player_enemy", "enemy_player", "hostile", "enemy")
PLAYER_ALLY_MARKERS = ("player_ally", "ally", "teammate", "friend", "party")


def detect_role(raw_id: str, explicit: str | None = None) -> str:
    if explicit in ("player_enemy", "player_ally", "monster"):
        return explicit
    rid = (raw_id or "").lower()
    if any(m in rid for m in PLAYER_ENEMY_MARKERS):
        return "player_enemy"
    if any(m in rid for m in PLAYER_ALLY_MARKERS):
        return "player_ally"
    return "monster"


def _valid_coord(x, y) -> bool:
    x, y = safe_float(x, -1), safe_float(y, -1)
    return 0 <= x <= 100000 and 0 <= y <= 100000


def _linearity(history) -> float:
    """净位移 / 累计路程：直线=1，原地乱窜→0（用于抖动惩罚）。"""
    if len(history) < 2:
        return 1.0
    path = 0.0
    prev = history[0]
    for cur in list(history)[1:]:
        path += ((cur["x"] - prev["x"]) ** 2 + (cur["y"] - prev["y"]) ** 2) ** 0.5
        prev = cur
    if path <= 1e-6:
        return 1.0
    first, last = history[0], history[-1]
    net = ((last["x"] - first["x"]) ** 2 + (last["y"] - first["y"]) ** 2) ** 0.5
    return max(0.0, min(1.0, net / path))


class _Tracker:
    def __init__(self, raw_id, rarity, role):
        self.raw_id, self.rarity, self.role = raw_id, rarity, role
        self.category = role if role != "monster" else classify_by_rarity(rarity)
        self.history = deque(maxlen=safe_int(cfg_get("predictor.history_maxlen", 10), 10))
        self.last_seen = time.time()

    def update(self, x, y):
        self.history.append({"t": time.time(), "x": float(x), "y": float(y)})
        self.last_seen = time.time()

    def expired(self) -> bool:
        return (time.time() - self.last_seen) > safe_float(cfg_get("predictor.entity_timeout", 0.4), 0.4)

    def _fit_linear(self, secs: float):
        """线性外推（原始模型，兜底）。"""
        oldest, latest = self.history[0], self.history[-1]
        dt = latest["t"] - oldest["t"]
        if dt <= 0:
            return None
        vx = (latest["x"] - oldest["x"]) / dt
        vy = (latest["y"] - oldest["y"]) / dt
        return {
            "x": latest["x"] + vx * secs,
            "y": latest["y"] + vy * secs,
            "vx": vx,
            "vy": vy,
            "model": "linear",
        }

    def _fit_accel(self, secs: float):
        """恒加速度模型（ROADMAP #7）：对相邻帧瞬时速度最小二乘拟合 v(t)=a*t+b，二次外推。

        加速度钳制在 ±predictor.accel_max，防止脏数据把外推炸飞。
        """
        h = list(self.history)
        if len(h) < 4:
            return None
        vs = []
        for a, b in zip(h, h[1:], strict=False):
            dt = b["t"] - a["t"]
            if dt <= 1e-6:
                continue
            # 平均速度的物理时刻是区间中点，用中点时间戳拟合才不引入半帧偏差
            vs.append(((a["t"] + b["t"]) / 2, (b["x"] - a["x"]) / dt, (b["y"] - a["y"]) / dt))
        if len(vs) < 3:
            return None
        t0 = vs[0][0]
        pts = [(t - t0, vx, vy) for t, vx, vy in vs]
        n = len(pts)
        st = sum(p[0] for p in pts)
        stt = sum(p[0] ** 2 for p in pts)
        den = n * stt - st * st
        if abs(den) < 1e-9:
            return None

        def fit(idx: int):
            sy = sum(p[idx] for p in pts)
            sty = sum(p[0] * p[idx] for p in pts)
            a = (n * sty - st * sy) / den
            b = (sy - a * st) / n
            return a, b

        ax, bx = fit(1)
        ay, by = fit(2)
        amax = safe_float(cfg_get("predictor.accel_max", 2000), 2000)
        ax = max(-amax, min(amax, ax))
        ay = max(-amax, min(amax, ay))
        last = h[-1]
        tau = last["t"] - t0  # 拟合 intercept 在 t0，当前速度须外推到最后一帧时刻
        vxn = bx + ax * tau
        vyn = by + ay * tau
        return {
            "x": last["x"] + vxn * secs + 0.5 * ax * secs * secs,
            "y": last["y"] + vyn * secs + 0.5 * ay * secs * secs,
            "vx": vxn + ax * secs,
            "vy": vyn + ay * secs,
            "model": "accel",
        }

    def _fit_circular(self, secs: float):
        """圆周/周期运动检测（ROADMAP #7）：代数最小二乘圆拟合 + 角速度沿弧外推。

        短弧窗口下质心法不稳，这里解 x²+y²+Dx+Ey+F=0 的线性方程组得圆心与半径；
        残差过大（不像圆）/ 半径过小 / 角速度过低均返回 None，由 auto 回落其他模型。
        """
        min_frames = max(6, safe_int(cfg_get("predictor.circular_min_frames", 8), 8))
        h = list(self.history)[-min_frames:]
        if len(h) < 6:
            return None
        n = len(h)
        # 先按质心中心化：浅弧段下原始坐标量级大、矩阵病态，中心化后数值稳定
        ox = sum(p["x"] for p in h) / n
        oy = sum(p["y"] for p in h) / n
        pts = [(p["x"] - ox, p["y"] - oy, p["t"]) for p in h]
        sx = sum(p[0] for p in pts)
        sy = sum(p[1] for p in pts)
        sxx = sum(p[0] * p[0] for p in pts)
        syy = sum(p[1] * p[1] for p in pts)
        sxy = sum(p[0] * p[1] for p in pts)
        sz = sum(p[0] * p[0] + p[1] * p[1] for p in pts)
        sxz = sum(p[0] * (p[0] * p[0] + p[1] * p[1]) for p in pts)
        syz = sum(p[1] * (p[0] * p[0] + p[1] * p[1]) for p in pts)

        def det3(a):
            return (
                a[0][0] * (a[1][1] * a[2][2] - a[1][2] * a[2][1])
                - a[0][1] * (a[1][0] * a[2][2] - a[1][2] * a[2][0])
                + a[0][2] * (a[1][0] * a[2][1] - a[1][1] * a[2][0])
            )

        m0 = [[sxx, sxy, sx], [sxy, syy, sy], [sx, sy, float(n)]]
        d0 = det3(m0)
        if abs(d0) < 1e-12:
            return None  # 严格奇异（共线）；浅弧病态解交给半径/残差兜底
        rhs = [-sxz, -syz, -sz]
        dd = (
            det3([[rhs[0], m0[0][1], m0[0][2]], [rhs[1], m0[1][1], m0[1][2]], [rhs[2], m0[2][1], m0[2][2]]])
            / d0
        )
        ee = (
            det3([[m0[0][0], rhs[0], m0[0][2]], [m0[1][0], rhs[1], m0[1][2]], [m0[2][0], rhs[2], m0[2][2]]])
            / d0
        )
        ff = (
            det3([[m0[0][0], m0[0][1], rhs[0]], [m0[1][0], m0[1][1], rhs[1]], [m0[2][0], m0[2][1], rhs[2]]])
            / d0
        )
        # 中心化坐标下的圆心换算回原坐标系
        cx, cy = ox - dd / 2, oy - ee / 2
        r_sq = (dd / 2) ** 2 + (ee / 2) ** 2 - ff
        if r_sq <= 0:
            return None
        radius = math.sqrt(r_sq)
        min_r = safe_float(cfg_get("predictor.circular_min_radius", 20), 20)
        if radius < min_r or radius > 100000:
            return None
        resid = sum(abs(math.hypot(p["x"] - cx, p["y"] - cy) - radius) for p in h) / n
        if resid / radius > 0.15:
            return None  # 不像圆（病态解也会在这里被拦下）
        dt_total = h[-1]["t"] - h[0]["t"]
        if dt_total <= 0:
            return None
        total_ang = 0.0
        last_a = None
        for p in h:
            a = math.atan2(p["y"] - cy, p["x"] - cx)
            if last_a is not None:
                d = a - last_a
                while d > math.pi:
                    d -= 2 * math.pi
                while d < -math.pi:
                    d += 2 * math.pi
                total_ang += d
            last_a = a
        omega = total_ang / dt_total
        min_w = safe_float(cfg_get("predictor.circular_min_omega", 0.3), 0.3)
        if abs(omega) < min_w:
            return None
        a_new = (last_a or 0.0) + omega * secs
        return {
            "x": cx + radius * math.cos(a_new),
            "y": cy + radius * math.sin(a_new),
            "vx": -radius * omega * math.sin(a_new),
            "vy": radius * omega * math.cos(a_new),
            "model": "circular",
        }

    def _choose_fit(self, secs: float):
        """ROADMAP #7 模型选择：predictor.model = auto|linear|accel|circular。

        auto = 回退一步实测残差选优：拿 history[:-1] 预测 history[-1]，
        谁的一步误差小就用谁做正式外推。
        """
        mode = str(cfg_get("predictor.model", "auto") or "auto").lower()
        fits = {"linear": self._fit_linear(secs)}
        if mode in ("accel", "auto"):
            fits["accel"] = self._fit_accel(secs)
        if mode in ("circular", "auto"):
            fits["circular"] = self._fit_circular(secs)
        if mode != "auto":
            return fits.get(mode) or fits["linear"]
        h = list(self.history)
        if len(h) >= 5:
            probe = _Tracker(self.raw_id, self.rarity, self.role)
            probe.history = deque(h[:-1], maxlen=len(h))
            last = h[-1]
            secs_back = max(1e-3, last["t"] - h[-2]["t"])
            best, best_err = None, None
            for name in ("linear", "accel", "circular"):
                fitter = getattr(probe, f"_fit_{name}")
                f = fitter(secs_back)
                if not f:
                    continue
                err = math.hypot(f["x"] - last["x"], f["y"] - last["y"])
                if best_err is None or err < best_err:
                    best, best_err = fitter(secs), err
            if best is not None:
                return best
        return fits["linear"]

    def predict(self) -> EntityPred | None:
        min_frames = safe_int(cfg_get("predictor.min_frames", 3), 3)
        if len(self.history) < min_frames:
            return None
        secs = safe_float(cfg_get("predictor.predict_seconds", 1.2), 1.2)
        fit = self._choose_fit(secs) or self._fit_linear(secs)
        if fit is None:
            return None
        latest = self.history[-1]
        vx, vy = fit["vx"], fit["vy"]
        px, py = fit["x"], fit["y"]
        frame_conf = min(
            1.0,
            len(self.history) / max(1.0, safe_float(cfg_get("predictor.frame_full_frames", 8), 8)),
        )
        speed = (vx**2 + vy**2) ** 0.5
        speed_pen = max(
            safe_float(cfg_get("predictor.speed_penalty_floor", 0.3), 0.3),
            1.0 - speed / safe_float(cfg_get("predictor.speed_ref", 2000.0), 2000.0),
        )
        if fit["model"] == "circular":
            jitter_pen = 1.0  # 周期轨迹的低直线度已被模型解释，不再按抖动惩罚
        else:
            jitter_pen = max(
                safe_float(cfg_get("predictor.jitter_floor", 0.35), 0.35),
                _linearity(self.history),
            )
        conf = round(frame_conf * speed_pen * jitter_pen, 3)
        trusted = conf >= safe_float(cfg_get("predictor.confidence_threshold", 0.65), 0.65)
        return {
            "raw_id": self.raw_id,
            "rarity": self.rarity,
            "category": self.category,
            "role": self.role,
            "threat_score": _threat_table().get(self.category, 5),
            "x_now": round(latest["x"], 1),
            "y_now": round(latest["y"], 1),
            "x_predict": round(px, 1) if trusted else None,
            "y_predict": round(py, 1) if trusted else None,
            "vx_per_sec": round(vx, 2),
            "vy_per_sec": round(vy, 2),
            "confidence": conf,
            "prediction_trusted": trusted,
            "model": fit["model"],
        }


class Predictor:
    """全实体历史追踪 + 预判。同 raw_id 多只时按上一帧最近距离匹配。"""

    def __init__(self):
        self._t = {}
        self._uid = 0

    def _predicted_pos(self, tk, now: float):
        """跟踪器在 now 时刻的预测位置（线性外推），返回 (px, py, vx, vy)。"""
        if not tk.history:
            return None
        last = tk.history[-1]
        vx = vy = 0.0
        if len(tk.history) >= 2:
            first = tk.history[0]
            dt_h = last["t"] - first["t"]
            if dt_h > 0:
                vx = (last["x"] - first["x"]) / dt_h
                vy = (last["y"] - first["y"]) / dt_h
        dt = min(max(now - last["t"], 0.0), 0.5)
        return last["x"] + vx * dt, last["y"] + vy * dt, vx, vy

    def _match(self, dets: list, now: float) -> dict:
        """ROADMAP #9：多因子匹配，返回 {det_index: tracker_uid}。

        打分 = 预测位置距离
             + 运动方向不一致惩罚（检测位移方向与跟踪器速度反向时按位移量加罚）
             + 角色跳变惩罚（敌↔友切换视为强误配信号）
        贪心按分低者优先独占分配；距离超过 match_max_dist 宁开新轨不误挂。
        同 raw_id 多只实体交叉走位时不再张冠李戴。
        """
        max_dist = safe_float(cfg_get("predictor.match_max_dist", 400), 400)
        pairs = []
        for di, d in enumerate(dets):
            for uid, tk in self._t.items():
                if tk.raw_id != d["raw_id"]:
                    continue
                pp = self._predicted_pos(tk, now)
                if pp is None:
                    continue
                px, py, vx, vy = pp
                dist = math.hypot(d["x"] - px, d["y"] - py)
                if dist > max_dist:
                    continue
                score = dist
                if tk.history:
                    last = tk.history[-1]
                    mx, my = d["x"] - last["x"], d["y"] - last["y"]
                    mnorm = math.hypot(mx, my)
                    vnorm = math.hypot(vx, vy)
                    if mnorm > 1 and vnorm > 1:
                        cos = (mx * vx + my * vy) / (mnorm * vnorm)
                        if cos < 0:
                            score += (1 - cos) * 0.5 * mnorm
                if (
                    tk.role in ("player_enemy", "player_ally")
                    and d["role"] in ("player_enemy", "player_ally")
                    and tk.role != d["role"]
                ):
                    score += 100000
                pairs.append((score, uid, di))
        pairs.sort(key=lambda p: p[0])
        used_t, used_d, assign = set(), set(), {}
        for _score, uid, di in pairs:
            if uid in used_t or di in used_d:
                continue
            used_t.add(uid)
            used_d.add(di)
            assign[di] = uid
        return assign

    def update(self, entities: list):
        now = time.time()
        dets = []
        for ent in entities or []:
            if not isinstance(ent, dict):
                continue
            raw_id = ent.get("raw_id", "unknown")
            x, y = ent.get("x"), ent.get("y")
            if not _valid_coord(x, y):
                continue
            dets.append(
                {
                    "raw_id": raw_id,
                    "rarity": ent.get("rarity", "Common"),
                    "role": detect_role(raw_id, ent.get("role")),
                    "x": safe_float(x),
                    "y": safe_float(y),
                }
            )
        assign = self._match(dets, now)
        for di, d in enumerate(dets):
            uid = assign.get(di)
            if uid is None:
                self._uid += 1
                uid = f"{d['raw_id']}_{self._uid}"
                self._t[uid] = _Tracker(d["raw_id"], d["rarity"], d["role"])
            tk = self._t[uid]
            tk.role = d["role"]
            tk.rarity = d["rarity"]
            tk.category = d["role"] if d["role"] != "monster" else classify_by_rarity(d["rarity"])
            tk.update(d["x"], d["y"])
        for uid in [u for u, t in self._t.items() if t.expired()]:
            del self._t[uid]

    def all_entities(self) -> list[EntityPred]:
        out = []
        for tk in self._t.values():
            p = tk.predict()
            if p is not None:
                out.append(p)
            elif tk.history:
                last = tk.history[-1]
                out.append(
                    {
                        "raw_id": tk.raw_id,
                        "rarity": tk.rarity,
                        "category": tk.category,
                        "role": tk.role,
                        "threat_score": _threat_table().get(tk.category, 5),
                        "x_now": round(last["x"], 1),
                        "y_now": round(last["y"], 1),
                        "x_predict": None,
                        "y_predict": None,
                        "vx_per_sec": 0,
                        "vy_per_sec": 0,
                        "confidence": 0.0,
                        "prediction_trusted": False,
                    }
                )
        out.sort(key=lambda e: e["threat_score"], reverse=True)
        return out[: safe_int(cfg_get("predictor.max_output_entities", 8), 8)]

    def reset(self):
        self._t.clear()
        self._uid = 0

    def status(self) -> dict:
        return {
            "tracked_entities": len(self._t),
            "predict_seconds": cfg_get("predictor.predict_seconds"),
            "confidence_threshold": cfg_get("predictor.confidence_threshold"),
        }


PREDICTOR = Predictor()


