# ===========================================================================
# 12. 视频学习（抽帧 → VLM 提取战术 → 去重入库）
# ===========================================================================
def _vlm_prompt() -> str:
    """VLM 提示词：档案/配置 llm.vlm_prompt 可覆盖（ROADMAP #14）。"""
    return str(cfg_get("llm.vlm_prompt", "") or "").strip() or VLM_PROMPT


VLM_PROMPT = (
    "你是游戏战术分析师。看图，用一句话总结一条可执行的战术，"
    "必须带上适用条件（例如「低血量时…」「被夹击时…」）。只输出这一句话。"
)


def _write_png(path: str, width: int, height: int, rgb: tuple) -> str:
    """标准库写纯色 PNG（无 cv2/PIL 时的最小可用实现）。"""
    import struct
    import zlib

    row = b"\x00" + bytes(rgb) * width
    raw = row * height

    def chunk(tag, data):
        return (
            struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(raw, 6))
    png += chunk(b"IEND", b"")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(png)
    return path


def synthesize_frames(count: int = 5) -> list:
    """合成帧（离线）：每帧颜色不同，保证去重/逐帧逻辑能被真实检验。"""
    os.makedirs(FRAME_DIR, exist_ok=True)
    out = []
    for i in range(max(0, int(count))):
        rgb = ((37 * i + 60) % 256, (91 * i + 30) % 256, (153 * i + 12) % 256)
        out.append(_write_png(os.path.join(FRAME_DIR, f"frame_{i:06d}.png"), 64, 48, rgb))
    return out


def extract_frames(video_path: str, skip: int = 25) -> list:
    """从视频按间隔抽帧；没装 cv2 返回空列表（上层会降级到合成帧）。"""
    try:
        import cv2
    except ImportError:
        log("[学习] 未安装 opencv（cv2），跳过真实抽帧")
        return []
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        log(f"[学习] 无法打开视频 {video_path}")
        return []
    os.makedirs(FRAME_DIR, exist_ok=True)
    frames, idx = [], 0
    while cap.isOpened():
        ok, frame = cap.read()
        if not ok:
            break
        if idx % skip == 0:
            p = os.path.join(FRAME_DIR, f"frame_{idx:06d}.jpg")
            cv2.imwrite(p, frame)
            frames.append(p)
        idx += 1
    cap.release()
    return frames


def _image_to_b64(path: str) -> str:
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


def vlm_extract_tactic(b64_img: str) -> str:
    """单帧 → 一句战术。无密钥时返回明确提示（不伪造）。"""
    url = (os.getenv("VLM_API_URL") or "").strip()
    key = (os.getenv("VLM_API_KEY") or "").strip()
    if not url or not key:
        return "[未配置 VLM_API_URL / VLM_API_KEY，跳过 VLM]"
    try:
        import requests

        payload: dict = {
            "model": os.getenv("VLM_MODEL", ""),
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64_img}"}},
                        {"type": "text", "text": _vlm_prompt()},
                    ],
                }
            ],
            "max_tokens": 200,
        }
        r = requests.post(
            url,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json=payload,
            timeout=30,
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        return f"[VLM 调用失败: {type(e).__name__}]"


def _useful_tactic(t: str) -> bool:
    t = (t or "").strip()
    return len(t) >= 2 and not t.startswith("[") and not t.startswith("（")


def _dedup_new(tactics: list, target_dir: str, ratio: float = 0.75) -> list:
    """与历史 video_tactic_*.md 里的逐条战术比对，剔除过于相似的。"""
    known = []
    if os.path.isdir(target_dir):
        for f in os.listdir(target_dir):
            if f.startswith("video_tactic_") and f.endswith(".md"):
                try:
                    with open(os.path.join(target_dir, f), encoding="utf-8") as fk:
                        known.append(fk.read())
                except OSError:
                    continue
    known_lines = []
    for line in "\n".join(known).splitlines():
        s = line.strip()
        for prefix in ("- ", "* "):
            if s.startswith(prefix):
                s = s[len(prefix) :].strip()
        if ". " in s[:5] and s[:1].isdigit():
            s = s.split(". ", 1)[1].strip()
        if s and not s.startswith(("#", ">", "!")):
            known_lines.append(s)
    out = []
    for t in tactics:
        if not _useful_tactic(t):
            continue
        if any(difflib.SequenceMatcher(None, t.strip(), k).ratio() >= ratio for k in known_lines):
            continue
        out.append(t.strip())
    return out


def _png_to_rgb(path: str):
    """极简 PNG 解码（仅 8-bit RGB/RGBA、非隔行）：无 cv2 时的降级路径。

    返回 (w, h, [(r,g,b), ...])；不支持的格式抛 ValueError。
    """
    import struct
    import zlib

    with open(path, "rb") as f:
        data = f.read()
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("not a PNG")
    pos = 8
    idat = []
    w = h = depth = ctype = None
    while pos + 8 <= len(data):
        (ln,) = struct.unpack(">I", data[pos : pos + 4])
        tag = data[pos + 4 : pos + 8]
        chunk = data[pos + 8 : pos + 8 + ln]
        if tag == b"IHDR":
            w, h, depth, ctype, _c, _f, interlace = struct.unpack(">IIBBBBB", chunk)
            if depth != 8 or ctype not in (2, 6) or interlace != 0:
                raise ValueError(f"unsupported PNG (depth={depth}, ctype={ctype})")
        elif tag == b"IDAT":
            idat.append(chunk)
        elif tag == b"IEND":
            break
        pos += 12 + ln
    if w is None or h is None or not idat:
        raise ValueError("broken PNG")
    raw = zlib.decompress(b"".join(idat))
    bpp = 3 if ctype == 2 else 4
    stride = w * bpp
    out = bytearray()
    prev = bytearray(stride)
    p = 0
    for _y in range(h):
        ft = raw[p]
        p += 1
        line = bytearray(raw[p : p + stride])
        p += stride
        if ft == 1:
            for i in range(bpp, stride):
                line[i] = (line[i] + line[i - bpp]) & 0xFF
        elif ft == 2:
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif ft == 3:
            for i in range(stride):
                a = line[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + ((a + prev[i]) >> 1)) & 0xFF
        elif ft == 4:
            for i in range(stride):
                a = line[i - bpp] if i >= bpp else 0
                b = prev[i]
                c = prev[i - bpp] if i >= bpp else 0
                pp = a + b - c
                pa, pb, pc = abs(pp - a), abs(pp - b), abs(pp - c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[i] = (line[i] + pr) & 0xFF
        elif ft != 0:
            raise ValueError(f"unknown filter {ft}")
        out += line
        prev = line
    pixels = [(out[i], out[i + 1], out[i + 2]) for i in range(0, len(out) - bpp + 1, bpp)]
    return w, h, pixels


def _uniform_hash(gray_avg: float) -> int:
    """纯色/近纯色图的退化哈希：直接编码平均灰度（重复 8 字节）。

    8×8 平均哈希对无梯度图恒为 0，会把不同颜色判成重复；退化分支保证
    「同色=距离0、异色=距离随灰度差增大」，合成帧与真实纯色场景都正确。
    """
    g = max(0, min(255, int(round(gray_avg))))
    bits = 0
    for i in range(8):
        bits |= g << (i * 8)
    return bits


def _avg_hash(path: str):
    """感知哈希（8×8 平均哈希 → 64bit）。cv2 优先，纯 Python PNG 解码兜底；
    都不可用返回 None（调用方保守保留该帧）。"""
    try:
        import cv2

        img = cv2.imread(path, cv2.IMREAD_GRAYSCALE)
        if img is not None:
            small = cv2.resize(img, (8, 8), interpolation=cv2.INTER_AREA)
            if float(small.max()) - float(small.min()) <= 1.0:
                return _uniform_hash(float(small.mean()))
            avg = float(small.mean())
            bits = 0
            for i, v in enumerate(small.flatten()):
                if float(v) > avg:
                    bits |= 1 << i
            return bits
    except Exception:
        pass
    try:
        w, h, pixels = _png_to_rgb(path)
    except (ValueError, OSError, EOFError):
        return None
    gray = [(r * 299 + g * 587 + b * 114) // 1000 for r, g, b in pixels]
    small = []
    for by in range(8):
        y0, y1 = by * h // 8, max(by * h // 8 + 1, (by + 1) * h // 8)
        for bx in range(8):
            x0, x1 = bx * w // 8, max(bx * w // 8 + 1, (bx + 1) * w // 8)
            vals = [gray[y * w + x] for y in range(y0, y1) for x in range(x0, x1)]
            small.append(sum(vals) // max(1, len(vals)))
    if max(small) - min(small) <= 1:
        return _uniform_hash(sum(small) / len(small))
    avg = sum(small) / len(small)
    bits = 0
    for i, v in enumerate(small):
        if v > avg:
            bits |= 1 << i
    return bits


def _hamming(a: int, b: int) -> int:
    return bin(int(a) ^ int(b)).count("1")


def _dedup_frames(paths: list) -> tuple:
    """ROADMAP #14：感知哈希跳过近重复帧，省 VLM 调用。返回 (保留帧, 跳过数)。"""
    if not bool(cfg_get("learn.hash_dedup", True)):
        return list(paths), 0
    threshold = safe_int(cfg_get("learn.hash_threshold", 5), 5)
    kept: list = []
    hashes: list = []
    skipped = 0
    for p in paths:
        h = _avg_hash(p)
        if h is None:  # 解码不了 → 保守保留
            kept.append(p)
            continue
        if any(_hamming(h, k) <= threshold for k in hashes):
            skipped += 1
            continue
        kept.append(p)
        hashes.append(h)
    return kept, skipped


def _vote_tactics(tactics: list, min_votes: int = 2) -> list:
    """ROADMAP #14：多帧投票。相似战术（ratio≥0.75）归组，票数达标才入库；
    代表句取组内最长一条（信息量最大）。min_votes=1 等价于关闭投票。"""
    groups: list = []  # [票数, 代表句]
    for t in tactics:
        t = str(t or "").strip()
        if not _useful_tactic(t):
            continue
        placed = False
        for g in groups:
            if difflib.SequenceMatcher(None, t, g[1]).ratio() >= 0.75:
                g[0] += 1
                if len(t) > len(g[1]):
                    g[1] = t
                placed = True
                break
        if not placed:
            groups.append([1, t])
    need = max(1, safe_int(min_votes, 2))
    return [g[1] for g in groups if g[0] >= need]


def learn_from_video(
    video_path: str | None = None, frame_count: int = 5, skip: int = 25, cleanup: bool = True
) -> dict:
    """一次完整学习（ROADMAP #14）：抽帧 → 哈希跳重复 → 逐帧 VLM → 多帧投票
    → 历史去重 → 入库 → 清理临时帧。"""
    frames = extract_frames(video_path, skip) if video_path else []
    mode = "真实抽帧" if frames else "合成帧(离线降级)"
    if not frames:
        frames = synthesize_frames(frame_count)
    total_frames = len(frames)
    frames, dedup_skipped = _dedup_frames(frames)
    log(f"[学习] {mode}，共 {total_frames} 帧（哈希去重跳过 {dedup_skipped}），开始逐帧提取战术...")

    tactics = []
    for fp in frames:
        try:
            t = vlm_extract_tactic(_image_to_b64(fp))
        except OSError as e:
            log(f"[学习] 读帧失败 {fp}: {e}")
            continue
        tactics.append(t)

    min_votes = safe_int(cfg_get("learn.min_votes", 2), 2)
    voted = _vote_tactics(tactics, min_votes)
    target = kb_game_dir(active_game())
    os.makedirs(target, exist_ok=True)
    kept = _dedup_new(voted, target)
    name = os.path.splitext(os.path.basename(video_path))[0] if video_path else "offline"
    fn = f"video_tactic_{safe_name(name)}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
    content = (
        f"# 视频学习战术 — {video_path or '(离线合成)'}\n\n"
        f"> 学习时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"> 提取帧数: {len(tactics)}（哈希去重跳过 {dedup_skipped}），"
        f"投票(≥{min_votes}帧)后 {len(voted)} 条，历史去重后 {len(kept)} 条\n\n"
        "## 战术列表\n\n" + "".join(f"{i}. {t}\n" for i, t in enumerate(kept, 1))
    )
    path = os.path.join(target, fn)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    if cleanup:
        shutil.rmtree(FRAME_DIR, ignore_errors=True)
    log_event(
        "learn",
        frames=len(tactics),
        dedup_skipped=dedup_skipped,
        voted=len(voted),
        kept=len(kept),
        file=path,
    )
    return {
        "frames": len(tactics),
        "dedup_skipped": dedup_skipped,
        "voted": len(voted),
        "kept": len(kept),
        "file": path,
    }


def download_video(url: str) -> str:
    """用 yt-dlp 下载视频（需自行安装 yt-dlp）。"""
    dest = os.path.join(FRAME_DIR, "_dl")
    os.makedirs(dest, exist_ok=True)
    import subprocess

    try:
        subprocess.run(
            [
                "yt-dlp",
                "-f",
                "b[ext=mp4]/bv*+ba/b",
                "-o",
                os.path.join(dest, "video.%(ext)s"),
                "--merge-output-format",
                "mp4",
                url,
            ],
            capture_output=True,
            text=True,
            timeout=300,
        )
    except Exception as e:
        return f"下载失败: {e}"
    for f in os.listdir(dest):
        if f.endswith(".mp4"):
            return os.path.join(dest, f)
    return "下载失败：未得到视频文件"


