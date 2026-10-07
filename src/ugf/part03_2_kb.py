# ===========================================================================
# 2. 知识库（Markdown）
# ===========================================================================
KB_HISTORY_DIRNAME = ".history"  # ROADMAP #12：写前快照存放目录（KB_DIR 内）

# ROADMAP #11：检索排序时永远置顶的权威知识文件（决策闭环依赖它们，
# 防止知识库被复盘/视频学习文件污染后，种子战术被 Top-N 挤出导致知识链断裂）
CANONICAL_KB_FILES = {"tactics.md", "_current_tactic.md", "boss_guide.md"}

KB_TEMPLATES = {
    "_README.md": (
        "# 本地知识库\n\n所有经验以 Markdown 存储，按游戏分区在 `knowledge_md/<游戏>/`。\n\n"
        "## 目录约定\n"
        "- `tactics.md` 战术（seed 自动生成，可手改）\n"
        "- `boss_guide.md` 高威胁目标指南（seed 自动生成）\n"
        "- `boss_behavior_log.md` BOSS 行为习惯（主循环自动追加）\n"
        "- `player_tactics.md` 换套/决策记录（主循环自动追加）\n"
        "- `review_*.md` 对局复盘（自动生成）\n"
        "- `video_tactic_*.md` 视频学习战术（自动生成）\n"
    ),
    "boss_behavior_log.md": (
        "# BOSS 行为日志\n\n记录遭遇 BOSS 时的行为习惯：移动模式 / 接近倾向 / 击杀或逃脱经验。\n\n"
        "（由 agent.py 主循环每 12 秒批量追加）\n"
    ),
    "player_tactics.md": "# 玩家打法笔记\n\n记录换套与决策轨迹，便于回看。\n",
}


def kb_ensure_templates():
    os.makedirs(KB_DIR, exist_ok=True)
    if not [f for f in os.listdir(KB_DIR) if f.endswith(".md")]:
        for fn, content in KB_TEMPLATES.items():
            p = os.path.join(KB_DIR, fn)
            if not os.path.exists(p):
                with open(p, "w", encoding="utf-8") as f:
                    f.write(content)


def kb_resolve(filename: str, game: str = "") -> str | None:
    """(文件名, 游戏名) → KB 内绝对路径；越界返回 None。"""
    sf = safe_name(filename)
    if not sf:
        return None
    if not sf.endswith(".md"):
        sf += ".md"
    root = KB_DIR
    if game:
        sg = safe_name(game)
        if sg:
            root = os.path.join(KB_DIR, sg)
            os.makedirs(root, exist_ok=True)
    full = os.path.normpath(os.path.join(root, sf))
    if os.path.commonpath([os.path.normpath(KB_DIR), full]) != os.path.normpath(KB_DIR):
        return None
    return full


def kb_list(game: str = "") -> list:
    base = kb_game_dir(game) if game else KB_DIR
    if not os.path.isdir(base):
        return []
    return sorted(f for f in os.listdir(base) if f.endswith(".md"))


def _tokenize(text: str) -> list:
    """BM25 轻量分词（ROADMAP #11）：ASCII 词 + 中文单字/二元组，零第三方依赖。"""
    text = str(text or "").lower()
    tokens = re.findall(r"[a-z0-9_]+", text)
    cjk = re.findall(r"[\u4e00-\u9fff]", text)
    tokens.extend(cjk)
    tokens.extend(a + b for a, b in zip(cjk, cjk[1:], strict=False))
    return tokens


def _bm25_scores(query_tokens: list, docs_tokens: list, k1: float = 1.5, b: float = 0.75) -> list:
    """经典 BM25 打分：docs_tokens 为每篇文档的 token 列表，返回每篇得分。"""
    n = len(docs_tokens)
    if not n or not query_tokens:
        return [0.0] * n
    avgdl = sum(len(d) for d in docs_tokens) / n or 1.0
    df: dict = {}
    for toks in docs_tokens:
        for t in set(toks):
            df[t] = df.get(t, 0) + 1
    scores = []
    for toks in docs_tokens:
        tf: dict = {}
        for t in toks:
            tf[t] = tf.get(t, 0) + 1
        s = 0.0
        dl = len(toks) or 1
        for q in set(query_tokens):
            f = tf.get(q, 0)
            if not f:
                continue
            idf = math.log(1 + (n - df.get(q, 0) + 0.5) / (df.get(q, 0) + 0.5))
            s += idf * (f * (k1 + 1)) / (f + k1 * (1 - b + b * dl / avgdl))
        scores.append(s)
    return scores


def _text_search(keyword: str, base: str) -> str:
    """知识库检索（ROADMAP #11）：字面命中保底 + BM25 相关性排序，返回 Top-N。

    排序规则：字面包含关键词的文档优先，其次按 BM25 得分降序；
    两者都不沾边的文档不返回。输出格式与旧版保持兼容（"共找到 N 条结果:"）。
    """
    if not os.path.isdir(base):
        return "未找到相关内容"
    kw = str(keyword or "").strip().lower()
    if not kw:
        return "未找到相关内容"
    docs: list = []  # (relpath, content, literal_hit)
    for root, _dirs, files in os.walk(base):
        if KB_HISTORY_DIRNAME in root.split(os.sep):
            continue
        for fn in sorted(f for f in files if f.endswith(".md")):
            fp = os.path.join(root, fn)
            try:
                with open(fp, encoding="utf-8") as f:
                    content = f.read()
            except (OSError, UnicodeDecodeError):  # 损坏/二进制文件跳过而非崩溃
                continue
            docs.append((os.path.relpath(fp, base), content, kw in content.lower()))
    if not docs:
        return "未找到相关内容"
    scores = _bm25_scores(_tokenize(kw), [_tokenize(c) for _r, c, _h in docs])
    top_n = max(1, safe_int(cfg_get("kb.search_top_n", 5), 5))

    def _tier(i: int) -> int:
        name = docs[i][0].replace(os.sep, "/").split("/")[-1]
        if docs[i][2] and name in CANONICAL_KB_FILES:
            return 2  # 权威战术文档且字面命中：永远置顶
        return 1 if docs[i][2] else 0

    ranked = sorted(range(len(docs)), key=lambda i: (_tier(i), scores[i]), reverse=True)
    hits: list = []
    for i in ranked:
        rel, content, literal = docs[i]
        if not literal and scores[i] <= 0:
            continue
        if len(hits) >= top_n:
            break
        tag = "" if literal else f"（相关度 {scores[i]:.2f}）"
        hits.append(f"## {rel}{tag}\n{content[:2000]}")
    if not hits:
        return "未找到相关内容"
    return f"共找到 {len(hits)} 条结果:\n" + "\n\n---\n\n".join(hits)


def kb_search(keyword: str, game: str = "") -> str:
    return _text_search(keyword, kb_game_dir(game) if game else KB_DIR)


def kb_write(filename: str, content: str, game: str = "") -> str:
    path = kb_resolve(filename, game)
    if path is None:
        return f"错误: 非法的知识库路径 (filename={filename!r}, game={game!r})"
    _kb_history_save(path, "write")
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(content or "")
    except OSError as e:
        return f"写入知识库失败: {e}"
    return f"已写入知识库: {path} ({len(content or '')} 字符)"


def kb_append(filename: str, content: str, game: str = "") -> str:
    path = kb_resolve(filename, game)
    if path is None:
        return f"错误: 非法的知识库路径 (filename={filename!r}, game={game!r})"
    _kb_history_save(path, "append")
    try:
        exists = os.path.exists(path)
        with open(path, "a" if exists else "w", encoding="utf-8") as f:
            if exists:
                f.write("\n\n")
            f.write(content or "")
    except OSError as e:
        return f"追加到知识库失败: {e}"
    return f"已追加到知识库: {path}"


def _kb_history_file(path: str) -> str:
    """ROADMAP #12：某知识库文件对应的历史 JSONL 路径（KB_DIR/.history/ 下）。"""
    rel = os.path.relpath(path, KB_DIR).replace(os.sep, "/")
    key = re.sub(r"[^\w.-]+", "_", rel)
    d = os.path.join(KB_DIR, KB_HISTORY_DIRNAME)
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, key + ".jsonl")


def _kb_history_save(path: str, op: str):
    """写前自动快照：整份正文追加进历史 JSONL，滚动保留最近 N 条。

    超过 kb.history_max_kb 的文件只记事件不存正文（防止历史目录膨胀）。
    """
    try:
        if not os.path.exists(path):
            return
        with open(path, encoding="utf-8") as f:
            content = f.read()
    except (OSError, UnicodeDecodeError):
        return
    max_kb = safe_int(cfg_get("kb.history_max_kb", 256), 256)
    if len(content.encode("utf-8")) > max_kb * 1024:
        content = ""
    rec = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "op": op,
        "size": len(content),
        "content": content,
    }
    hf = _kb_history_file(path)
    try:
        lines = []
        if os.path.exists(hf):
            with open(hf, encoding="utf-8") as f:
                lines = f.read().splitlines()
        lines.append(json.dumps(rec, ensure_ascii=False))
        cap = max(1, safe_int(cfg_get("kb.history_revisions", 20), 20))
        with open(hf, "w", encoding="utf-8") as f:
            f.write("\n".join(lines[-cap:]) + "\n")
    except OSError:
        pass


def _kb_history_rows(path: str) -> list:
    hf = _kb_history_file(path)
    if not os.path.exists(hf):
        return []
    rows = []
    try:
        with open(hf, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        return []
    return rows


def kb_history(filename: str, game: str = "") -> str:
    """查看某知识库文件的历史修订列表（新→旧）。"""
    path = kb_resolve(filename, game)
    if path is None:
        return f"错误: 非法的知识库路径 (filename={filename!r}, game={game!r})"
    rows = _kb_history_rows(path)
    if not rows:
        return f"{filename} 暂无历史修订"
    out = [f"{filename} 共 {len(rows)} 条历史修订（新→旧）:"]
    for i, r in enumerate(reversed(rows)):
        out.append(f"  rev -{i + 1}: {r.get('ts')} op={r.get('op')} size={r.get('size')}")
    return "\n".join(out)


def kb_rollback(filename: str, rev: int = 1, game: str = "") -> str:
    """回滚到第 rev 新的历史修订（rev=1 即上一次写入前的状态）。

    回滚前会把当前内容也存进历史，因此回滚本身可再回滚。
    """
    path = kb_resolve(filename, game)
    if path is None:
        return f"错误: 非法的知识库路径 (filename={filename!r}, game={game!r})"
    rows = _kb_history_rows(path)
    if not rows:
        return f"{filename} 暂无历史修订，无法回滚"
    rev = max(1, safe_int(rev, 1))
    idx = len(rows) - rev
    if idx < 0:
        return f"修订号超出范围（共 {len(rows)} 条历史）"
    content = rows[idx].get("content")
    if content is None:
        return "该修订未存正文（超大文件只记事件），无法回滚"
    _kb_history_save(path, "pre-rollback")
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
    except OSError as e:
        return f"回滚写入失败: {e}"
    return f"已回滚 {filename} 到 rev -{rev}（{rows[idx].get('ts')}，{len(content)} 字符）"


def kb_query_boss(boss_name: str = "") -> str:
    path = os.path.join(KB_DIR, "boss_behavior_log.md")
    if not os.path.exists(path):
        return "知识库还没有 BOSS 行为记录(文件不存在)。"
    try:
        with open(path, encoding="utf-8") as f:
            content = f.read()
    except OSError as e:
        return f"读取失败: {e}"
    if not boss_name:
        return content or "知识库还没有 BOSS 行为记录。"
    hits = [seg for seg in content.split("### ") if boss_name.lower() in seg.lower()]
    return (
        "\n\n".join(f"### {s}" for s in hits)
        if hits
        else f"知识库中没有关于「{boss_name}」的 BOSS 行为记录。"
    )


def kb_boss_ranking(top: int = 5, game: str = "") -> str:
    """ROADMAP v2 #9：BOSS 危险度排行——遭遇次数（行为日志）+ 致死次数（复盘结构化字段）。"""
    from collections import Counter

    g = safe_name(game or active_game()) or "default"
    encounters: Counter = Counter()
    log_path = kb_resolve("boss_behavior_log", g)
    if log_path and os.path.exists(log_path):
        try:
            with open(log_path, encoding="utf-8") as f:
                for line in f:
                    m = re.match(r"^- (\d{2}:\d{2}:\d{2}) (.+?) 位置", line.strip())
                    if m:
                        encounters[m.group(2)] += 1
        except (OSError, UnicodeDecodeError):
            pass
    deaths: Counter = Counter()
    d = kb_game_dir(g)
    if os.path.isdir(d):
        for fn in sorted(os.listdir(d)):
            if not (fn.startswith("review_") and fn.endswith(".md")):
                continue
            try:
                with open(os.path.join(d, fn), encoding="utf-8") as f:
                    content = f.read()
            except (OSError, UnicodeDecodeError):
                continue
            if "- outcome: 死亡" not in content:
                continue
            m = re.search(r"- killer_entities: (.+)", content)
            if m:
                for ent in m.group(1).split("、"):
                    ent = ent.strip()
                    if ent and ent != "未知":
                        deaths[ent] += 1
    names = set(encounters) | set(deaths)
    if not names:
        return f"（{g}）暂无 BOSS 遭遇记录——打几局带 BOSS 的对局后再来看排行"
    rows = sorted(names, key=lambda n: (deaths.get(n, 0), encounters.get(n, 0)), reverse=True)
    n_top = max(1, safe_int(top, 5))
    lines = [f"== BOSS 危险度排行（{g} · Top {min(n_top, len(rows))} / 共 {len(rows)} 种）==",
             f"{'实体':<30}{'遭遇':>6}{'致死':>6}"]
    for n in rows[:n_top]:
        lines.append(f"{n:<30}{encounters.get(n, 0):>6}{deaths.get(n, 0):>6}")
    return "\n".join(lines)


def kb_stats(game: str = "", all_games: bool = False) -> str:
    """ROADMAP v2 #10：知识库规模与变动统计（文件/体积/最大文件/修订/回滚/学习）。"""
    if all_games and os.path.isdir(KB_DIR):
        games = [
            d for d in sorted(os.listdir(KB_DIR))
            if os.path.isdir(os.path.join(KB_DIR, d)) and d != KB_HISTORY_DIRNAME
        ] or ["default"]
    else:
        games = [safe_name(game or active_game()) or "default"]
    lines = []
    for g in games:
        d = kb_game_dir(g)
        if not os.path.isdir(d):
            lines.append(f"[{g}] （空分区）")
            continue
        files = [f for f in os.listdir(d) if f.endswith(".md")]
        sizes = []
        for f in files:
            try:
                sizes.append((os.path.getsize(os.path.join(d, f)), f))
            except OSError:
                pass
        sizes.sort(reverse=True)
        n_hist = n_roll = 0
        hist_dir = os.path.join(KB_DIR, KB_HISTORY_DIRNAME)
        prefix = re.sub(r"[^\w.-]+", "_", g) + "_"
        if os.path.isdir(hist_dir):
            for hf in os.listdir(hist_dir):
                if not hf.endswith(".jsonl") or not hf.startswith(prefix):
                    continue
                try:
                    with open(os.path.join(hist_dir, hf), encoding="utf-8") as f:
                        for line in f:
                            if not line.strip():
                                continue
                            n_hist += 1
                            if '"pre-rollback"' in line or '"rollback"' in line:
                                n_roll += 1
                except OSError:
                    pass
        lines.append(f"[{g}] {len(files)} 篇 / {_dir_mb(d):.3f} MB ｜ 历史修订 {n_hist} 条（含回滚快照 {n_roll}）")
        for size, fn in sizes[:5]:
            lines.append(f"    {size / 1024:8.1f} KB  {fn}")
    archive_mb = _dir_mb(ARCHIVE_DIR) if os.path.isdir(ARCHIVE_DIR) else 0.0
    lines.append(f"[归档] {archive_mb:.3f} MB ｜ [知识库合计] {_dir_mb(KB_DIR) if os.path.isdir(KB_DIR) else 0.0:.3f} MB")
    learns = read_events(10**9, "learn")
    if learns:
        kept_total = sum(safe_int(e.get("kept")) for e in learns)
        lines.append(f"[学习] 视频学习 {len(learns)} 次 / 累计入库 {kept_total} 条 / 最近 {learns[-1].get('ts')}")
    return "\n".join(lines)


def kb_switch_tactic(tactic_file: str) -> str:
    name = safe_name(tactic_file)
    if not name:
        return f"错误: 非法的战术文件名: {tactic_file!r}"
    if not name.endswith(".md"):
        name += ".md"
    src = os.path.join(KB_DIR, name)
    if not os.path.exists(src):
        return f"知识库中没有这份战术文档: {name}"
    try:
        with open(src, encoding="utf-8") as f:
            content = f.read()
        with open(os.path.join(KB_DIR, "_current_tactic.md"), "w", encoding="utf-8") as f:
            f.write(f"# 当前战术: {name}\n\n来自: {name}\n\n{content[:2000]}")
    except OSError as e:
        return f"切换战术失败: {e}"
    return f"已切换当前战术为: {name}"


def kb_export() -> str:
    """把活跃库 + 归档库打包成 kb_backups/kb_backup_<时间>.tar.gz。"""
    os.makedirs(BACKUP_DIR, exist_ok=True)
    path = os.path.join(BACKUP_DIR, f"kb_backup_{time.strftime('%Y%m%d_%H%M%S')}.tar.gz")
    n = 0
    with tarfile.open(path, "w:gz") as tar:
        for arc_root, src in (("knowledge_md", KB_DIR), ("knowledge_archive", ARCHIVE_DIR)):
            if not os.path.isdir(src):
                continue
            for root, _d, files in os.walk(src):
                for fn in sorted(f for f in files if f.endswith(".md")):
                    full = os.path.join(root, fn)
                    rel = os.path.relpath(full, src).replace(os.sep, "/")
                    rel = "/".join(p for p in rel.split("/") if p not in ("", ".", ".."))
                    if not rel:
                        continue
                    tar.add(full, arcname=f"{arc_root}/{rel}")
                    n += 1
    size = os.path.getsize(path) / 1024
    return f"已导出知识库到: {path}（{n} 个文件，{size:.1f} KB）"


def kb_import(backup_path: str) -> str:
    if not os.path.isabs(backup_path):
        backup_path = os.path.join(BASE_DIR, backup_path)
    if not os.path.isfile(backup_path):
        return f"找不到备份文件: {backup_path}"
    os.makedirs(KB_DIR, exist_ok=True)
    os.makedirs(ARCHIVE_DIR, exist_ok=True)
    roots = {"knowledge_md": KB_DIR, "knowledge_archive": ARCHIVE_DIR}
    count = {"knowledge_md": 0, "knowledge_archive": 0}
    with tarfile.open(backup_path, "r:gz") as tar:
        for m in tar.getmembers():
            if not m.isfile() or not m.name.endswith(".md"):
                continue
            parts = [p for p in m.name.replace("\\", "/").split("/") if p not in ("", ".", "..")]
            if len(parts) < 2:
                continue
            key = parts[0] if parts[0] in roots else "knowledge_md"
            dest_root = roots[key]
            dest = os.path.normpath(os.path.join(dest_root, *parts[1:]))
            if os.path.commonpath([os.path.normpath(dest_root), dest]) != os.path.normpath(dest_root):
                continue  # 防路径穿越
            try:
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                fp = tar.extractfile(m)
                if fp is None:
                    continue
                with open(dest, "wb") as out:
                    out.write(fp.read())
                count[key] += 1
            except (OSError, tarfile.TarError):
                continue
    msg = f"已从备份恢复 {count['knowledge_md']} 个知识库文件到 knowledge_md/"
    if count["knowledge_archive"]:
        msg += f"，{count['knowledge_archive']} 个归档文件到 knowledge_archive/"
    return msg


def kb_compress_duplicates(ratio: float = 0.9) -> int:
    """合并高度相似的 video_tactic_*.md（保留更长的一份）。"""
    if not os.path.isdir(KB_DIR):
        return 0
    files = sorted(f for f in os.listdir(KB_DIR) if f.startswith("video_tactic_") and f.endswith(".md"))
    removed = 0
    i = 0
    while i < len(files):
        pa = os.path.join(KB_DIR, files[i])
        try:
            with open(pa, encoding="utf-8") as fa:
                ta = fa.read()
        except OSError:
            i += 1
            continue
        j = i + 1
        while j < len(files):
            pb = os.path.join(KB_DIR, files[j])
            try:
                with open(pb, encoding="utf-8") as fb:
                    tb = fb.read()
            except OSError:
                j += 1
                continue
            if difflib.SequenceMatcher(None, ta, tb).ratio() >= ratio:
                keep, drop = (pa, pb) if len(ta) >= len(tb) else (pb, pa)
                try:
                    os.remove(drop)
                    removed += 1
                except OSError:
                    pass
                files.pop(j if drop == pb else i)
                continue
            j += 1
        i += 1
    return removed


def _dir_mb(path: str) -> float:
    total = 0
    for root, _d, files in os.walk(path):
        for fn in files:
            with contextlib.suppress(OSError):
                total += os.path.getsize(os.path.join(root, fn))
    return total / (1024 * 1024)


def kb_archive_oldest(max_mb: float) -> int:
    """活跃库超上限时，把最旧的笔记移进归档目录。"""
    os.makedirs(ARCHIVE_DIR, exist_ok=True)
    moved = 0
    while _dir_mb(KB_DIR) > max_mb:
        files = [f for f in os.listdir(KB_DIR) if f.endswith(".md")]
        if not files:
            break
        oldest = min(files, key=lambda f: os.path.getmtime(os.path.join(KB_DIR, f)))
        try:
            shutil.move(os.path.join(KB_DIR, oldest), os.path.join(ARCHIVE_DIR, oldest))
            moved += 1
        except OSError:
            break
    return moved


def kb_maintain() -> str:
    """体积维护：归档超限 + 合并重复（主循环启动时调用）。"""
    archived = kb_archive_oldest(safe_float(cfg_get("agent.kb_max_mb", 50), 50))
    merged = kb_compress_duplicates()
    if archived or merged:
        return f"知识库维护：归档 {archived} 份、合并重复 {merged} 份"
    return "知识库维护：无需处理"


def kb_clean(target: str = "all") -> str:
    """= 原 clean_cache：清预判历史 / 临时帧目录。"""
    done = []
    if target in ("all", "predict"):
        PREDICTOR.reset()
        done.append("预判历史已清空")
    if target in ("all", "frames"):
        if os.path.isdir(FRAME_DIR):
            shutil.rmtree(FRAME_DIR, ignore_errors=True)
            done.append("临时帧目录已清理" if not os.path.isdir(FRAME_DIR) else "临时帧目录清理失败")
        else:
            done.append("临时帧目录不存在（无需清理）")
    return "; ".join(done) if done else f"未知目标: {target}，可选 all/predict/frames"


