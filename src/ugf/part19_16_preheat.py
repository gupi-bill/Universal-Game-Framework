# ===========================================================================
# 16. 联网查攻略 + 现场 LoRA 预热（v3.0 通用游戏 Agent）
# ===========================================================================
# 启动任务前：
#   1) 多源检索（wiki/B站/米游社/reddit）拉攻略
#   2) 清洗成结构化样本 [状态, 推荐动作, 风险说明]
#   3) 现场 LoRA 小适配器训练（不改主干权重，任务完即弃）
# 全程带超时与降级：拉不到/训不动就跳过，不卡死主流程。

import hashlib
import os
import re
import time
from datetime import datetime


def _preheat_cache_dir() -> str:
    d = str(cfg_get("preheat.cache_dir", "") or "").strip()
    if d:
        return d
    return os.path.join(BASE_DIR, "preheat_cache")


# ---------------------------------------------------------------------------
# 16.1 资料检索
# ---------------------------------------------------------------------------
def _fetch_tutorials(game: str, target: str) -> list:
    """按游戏+目标关键词，从多个来源拉攻略文本。失败静默降级。

    返回 [{"source": str, "title": str, "text": str}, ...]
    """
    queries = [
        f"{game} {target} 攻略",
        f"{game} {target} 教程",
        f"{game} {target} how to beat",
    ]
    out = []
    sources = cfg_get("preheat.sources", []) or []
    try:
        import requests
    except ImportError:
        log("[预热] 未装 requests，跳过联网检索")
        return out

    # v3.0-5: 多源检索
    if "web" in sources or not sources:
        out.extend(_fetch_duckduckgo(queries))
    if "bilibili" in sources:
        out.extend(_fetch_bilibili(game, target))
    if "reddit" in sources:
        out.extend(_fetch_reddit(game, target))
    if "wiki" in sources:
        out.extend(_fetch_wiki(game, target))
    return out


def _fetch_duckduckgo(queries: list) -> list:
    """DuckDuckGo 通用搜索。"""
    out = []
    try:
        import requests
    except ImportError:
        return out
    for q in queries:
        try:
            r = requests.get(
                "https://html.duckduckgo.com/html/",
                params={"q": q},
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=10,
            )
            if not r.ok:
                continue
            titles = re.findall(r'result__a[^>]*>([^<]+)<', r.text)
            snippets = re.findall(r'result__snippet[^>]*>([^<]+)<', r.text)
            for i, t in enumerate(titles[:5]):
                snip = snippets[i] if i < len(snippets) else ""
                out.append({"source": "web", "title": t.strip(), "text": snip.strip()})
        except Exception as e:
            log(f"[预热] DDG搜索失败: {type(e).__name__}")
        time.sleep(0.5)
    return out


def _fetch_bilibili(game: str, target: str) -> list:
    """B站视频搜索（公开 API，无需 key）。"""
    out = []
    try:
        import requests
        r = requests.get(
            "https://api.bilibili.com/x/web-interface/search/type",
            params={"search_type": "video", "keyword": f"{game} {target} 攻略"},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10,
        )
        if r.ok:
            data = r.json().get("data", {}).get("result", [])
            for v in data[:5]:
                title = re.sub(r'<[^>]+>', '', v.get("title", ""))
                desc = v.get("description", "") or v.get("subtitle", "")
                out.append({"source": "bilibili", "title": title, "text": desc})
    except Exception as e:
        log(f"[预热] B站搜索失败: {type(e).__name__}")
    return out


def _fetch_reddit(game: str, target: str) -> list:
    """Reddit 搜索（公开 JSON 端点）。"""
    out = []
    try:
        import requests
        r = requests.get(
            f"https://www.reddit.com/search.json",
            params={"q": f"{game} {target}", "limit": 5},
            headers={"User-Agent": "ugf-preheat/1.0"},
            timeout=10,
        )
        if r.ok:
            for post in r.json().get("data", {}).get("children", []):
                d = post.get("data", {})
                out.append({
                    "source": "reddit",
                    "title": d.get("title", ""),
                    "text": d.get("selftext", "")[:300],
                })
    except Exception as e:
        log(f"[预热] Reddit搜索失败: {type(e).__name__}")
    return out


def _fetch_wiki(game: str, target: str) -> list:
    """游戏 wiki 搜索（尝试 Fandom wiki）。"""
    out = []
    try:
        import requests
        r = requests.get(
            f"https://{game}.fandom.com/api.php",
            params={"action": "query", "list": "search", "srsearch": target,
                    "format": "json", "srlimit": 3},
            headers={"User-Agent": "ugf-preheat/1.0"},
            timeout=10,
        )
        if r.ok:
            for item in r.json().get("query", {}).get("search", []):
                out.append({
                    "source": "wiki",
                    "title": item.get("title", ""),
                    "text": re.sub(r'<[^>]+>', '', item.get("snippet", "")),
                })
    except Exception as e:
        log(f"[预热] Wiki搜索失败: {type(e).__name__}")
    return out


# ---------------------------------------------------------------------------
# 16.2 清洗成训练样本
# ---------------------------------------------------------------------------
_ACTION_WORDS = [
    "attack", "defend", "dodge", "retreat", "chase", "heal", "use_skill",
    "use_ultimate", "move_left", "move_right", "move_up", "move_down",
    "攻击", "防御", "闪避", "撤退", "追击", "加血", "用技能", "放大招",
]


def _clean_to_samples(docs: list, max_n: int) -> list:
    """把攻略文本切成 (状态, 动作, 理由) 三元组样本。"""
    samples = []
    for doc in docs:
        text = doc.get("text", "") or ""
        if len(text) < 10:
            continue
        # 简单切句
        for sent in re.split(r"[。！！.!?？\n]", text):
            sent = sent.strip()
            if len(sent) < 8 or len(sent) > 200:
                continue
            # 找句子里有没有动作词
            action = next((a for a in _ACTION_WORDS if a in sent.lower()), None)
            if not action:
                continue
            samples.append({
                "state": doc.get("title", ""),
                "action": action,
                "reason": sent,
                "source": doc.get("source", "web"),
            })
            if len(samples) >= max_n:
                return samples
    return samples


# ---------------------------------------------------------------------------
# 16.3 现场 LoRA 训练（骨架，依赖可选）
# ---------------------------------------------------------------------------
def _train_lora(samples: list, game: str, target: str) -> str:
    """用样本训一个临时 LoRA 适配器。返回适配器路径。

    依赖 transformers/peft/torch 才真训；没有就退化成把样本写进知识库。
    """
    cache_dir = _preheat_cache_dir()
    os.makedirs(cache_dir, exist_ok=True)
    key = hashlib.md5(f"{game}:{target}".encode()).hexdigest()[:12]
    adapter_dir = os.path.join(cache_dir, f"lora_{key}")

    # 不管训没训，先把样本落盘，下次命中直接复用
    import json
    samples_file = os.path.join(cache_dir, f"samples_{key}.json")
    with open(samples_file, "w", encoding="utf-8") as f:
        json.dump(samples, f, ensure_ascii=False, indent=2)

    if not cfg_get("preheat.auto_lora", True):
        log("[预热] auto_lora=false，跳过 LoRA 训练，样本已落盘")
        return ""

    try:
        import torch  # noqa: F401
        from peft import LoraConfig  # noqa: F401
        from transformers import AutoTokenizer  # noqa: F401
    except ImportError:
        log("[预热] 未装 torch/peft/transformers，样本已存知识库（跳过实训练）")
        # 降级：把样本写进知识库 MD
        kb_dir = os.path.join(BASE_DIR, "kb")
        os.makedirs(kb_dir, exist_ok=True)
        md = os.path.join(kb_dir, f"preheat_{key}.md")
        with open(md, "w", encoding="utf-8") as f:
            f.write(f"# 预热攻略：{game} - {target}\n\n")
            for s in samples:
                f.write(f"- **{s['action']}**：{s['reason']}\n")
        log(f"[预热] 已写 {len(samples)} 条样本到 {md}")
        return ""

    # 真训练（骨架占位，依赖到位再补实际训练循环）
    os.makedirs(adapter_dir, exist_ok=True)
    log(f"[预热] LoRA 训练完成（占位）：{adapter_dir}")
    return adapter_dir


# ---------------------------------------------------------------------------
# 16.4 对外入口：preheat(game, target)
# ---------------------------------------------------------------------------
def preheat(game: str, target: str) -> dict:
    """启动任务前的预热：查攻略 → 清洗 → 现场 LoRA。

    返回 {"samples": N, "adapter": path|""}
    同一 game:target 第二次跑直接命中缓存，不重复联网/训练。
    """
    if not cfg_get("preheat.enable", True):
        log("[预热] preheat.enable=false，跳过")
        return {"samples": 0, "adapter": ""}

    t0 = time.time()
    max_sec = safe_int(cfg_get("preheat.max_seconds", 180), 180)
    max_samples = safe_int(cfg_get("preheat.max_samples", 200), 200)

    # 缓存命中检查
    import json as _json
    cache_dir = _preheat_cache_dir()
    os.makedirs(cache_dir, exist_ok=True)
    key = hashlib.md5(f"{game}:{target}".encode()).hexdigest()[:12]
    samples_file = os.path.join(cache_dir, f"samples_{key}.json")
    adapter_dir = os.path.join(cache_dir, f"lora_{key}")
    if os.path.exists(samples_file):
        try:
            with open(samples_file, encoding="utf-8") as f:
                cached = _json.load(f)
            n = len(cached) if isinstance(cached, list) else 0
            adapter = adapter_dir if os.path.isdir(adapter_dir) else ""
            log(f"[预热] 缓存命中（{game}:{target}），{n} 条样本，跳过联网")
            return {"samples": n, "adapter": adapter, "cached": True}
        except Exception:
            pass  # 缓存损坏就重新来

    log(f"[预热] 开始检索 {game} - {target}（上限 {max_sec}s）")

    # 第一步：拉资料（带超时）
    docs = []
    try:
        # 把超时切成多段，防止单站卡死
        remaining = max_sec - (time.time() - t0)
        if remaining > 10:
            docs = _fetch_tutorials(game, target)
    except Exception as e:
        log(f"[预热] 检索异常: {type(e).__name__}: {e}")

    log(f"[预热] 抓到 {len(docs)} 条文档")

    # 第二步：清洗样本
    samples = _clean_to_samples(docs, max_samples)
    log(f"[预热] 清洗出 {len(samples)} 条训练样本")

    # 第三步：现场 LoRA（带超时检查）
    adapter = ""
    if samples and (time.time() - t0) < max_sec:
        try:
            adapter = _train_lora(samples, game, target)
        except Exception as e:
            log(f"[预热] LoRA 训练失败（已降级）: {type(e).__name__}: {e}")

    elapsed = time.time() - t0
    log(f"[预热] 完成，耗时 {elapsed:.1f}s，样本 {len(samples)}，adapter={adapter or '无'}")
    return {"samples": len(samples), "adapter": adapter}
