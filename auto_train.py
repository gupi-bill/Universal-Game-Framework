#!/usr/bin/env python3
"""传游戏名 → 自动搜资料 → 准备数据 → 训练 → 产出权重。

用法（一条命令）：
    python auto_train.py "florr.io"
    python auto_train.py "Terraria" --epochs 50

流程：
    1. 搜资料  → wiki / Fandom / GitHub，收集怪物名、稀有度、属性
    2. 判定数据来源（三种，自动选）：
       a. 本地已有数据集   → 直接用
       b. 游戏有公开素材   → 下载素材 + 合成标注数据
       c. 都没有           → 停下，报告缺什么（不瞎编）
    3. 训练      → YOLO，CPU/GPU 自动选
    4. 产出      → models/<game>_detector.pt

设计原则：**宁可停下来报错，也不产出假数据。**
之前踩过的坑：合成数据能训出模型，但和真实画面差得远，
看着 mAP 很高却完全不能用——这比直接失败更糟。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"}

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_ROOT = os.path.join(BASE, "datasets")
MODELS_DIR = os.path.join(BASE, "models")
LOG_DIR = os.path.join(BASE, "training_logs")

# 已知的素材源（按游戏名匹配）
KNOWN_SOURCES = {
    "florr.io": {
        "kind": "synthetic",
        "repo": "PANP2010/florr_powerful_tools",
        "note": "mob_images/ + backgrounds/ 可合成标注数据",
    },
}


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


# ---------------------------------------------------------------------------
# 1. 搜资料
# ---------------------------------------------------------------------------
def search(game: str) -> dict:
    """搜该游戏的资料。重点是拿到「实体名 + 稀有度」清单。"""
    import requests
    out = {"game": game, "refs": [], "entities": [], "summary": ""}

    print(f"\n[1/4] 搜索资料: {game}")

    # 官方静态数据（florr 这类游戏有 mobs.txt / rarities.txt）
    for path in ("mobs.txt", "rarities.txt"):
        for loc in ("en_US", "zh_CN"):
            url = f"https://florr.io/static/i18n/{loc}/{path}"
            try:
                r = requests.get(url, headers=UA, timeout=8)
                if r.status_code == 200 and len(r.text) > 100:
                    out["refs"].append(url)
                    if path == "mobs.txt":
                        out["entities"] = sorted(set(re.findall(
                            r"Mobs/([a-z_0-9]+)/Name=", r.text)))
                    print(f"  ✓ {url} → {len(out['entities'])} 个实体")
                    break
            except Exception:
                pass

    # GitHub 找素材仓库
    for q in (f"{game} mobs dataset", f"{game} assets sprites"):
        try:
            url = ("https://api.github.com/search/repositories?q="
                   + requests.utils.quote(q) + "&per_page=5")
            r = requests.get(url, headers=UA, timeout=8)
            if r.status_code == 200:
                for it in r.json().get("items", []):
                    out["refs"].append(it["html_url"])
        except Exception:
            pass

    if not out["refs"]:
        print("  ✗ 没搜到任何资料")
    else:
        print(f"  ✓ 共 {len(out['refs'])} 个来源，{len(out['entities'])} 个实体名")
    return out


# ---------------------------------------------------------------------------
# 2. 判定数据来源
# ---------------------------------------------------------------------------
def resolve_data(game: str, info: dict) -> dict:
    """决定用哪份数据训练。三种来源自动判定，找不到就返回错误。"""
    key = slug(game)
    print(f"\n[2/4] 准备数据")

    # a. 本地已有
    local = os.path.join(DATA_ROOT, key)
    if os.path.isdir(local):
        n = sum(len([f for f in fs if f.endswith((".jpg", ".png"))])
                for _, _, fs in os.walk(local))
        if n > 0:
            print(f"  ✓ 用本地已有数据集: {local}（{n} 张图）")
            return {"kind": "local", "dir": local, "images": n}

    # b. 已知可合成
    for g, cfg in KNOWN_SOURCES.items():
        if slug(g) == key and cfg["kind"] == "synthetic":
            print(f"  → 该游戏有公开素材（{cfg['note']}）")
            print(f"    下载 {cfg['repo']} 并合成…")
            ok = _synth_from_repo(cfg["repo"], local)
            if ok:
                return {"kind": "synthesized", "dir": local}
            print("  ✗ 素材下载/合成失败")
            return {"error": "素材下载或合成失败"}

    # c. 没有
    print("  ✗ 找不到可用数据源")
    print(f"    搜到的来源：{info['refs'][:3] or '无'}")
    print()
    print("  这个游戏没有可直接使用的公开标注数据。")
    print("  可行做法（按成本排序）：")
    print("   1. 录屏 + 抽帧 + 手工标注（Kaggle/Roboflow 上有现成流程）")
    print("   2. 找该游戏的 mod/clone 仓库，通常自带素材，可合成")
    print("   3. 纯模板匹配，不训模型（见 tools/template_matcher.py）")
    return {"error": "无可用数据源"}


def _synth_from_repo(repo: str, dest: str) -> bool:
    """从已知仓库拉素材并合成标注数据。"""
    import requests
    # GitHub 大文件链路不稳（实测反复 ConnectTimeout / TLS 断连），
    # 所以多试几个源，且把重试间隔拉长。
    sources = [
        f"https://github.com/{repo}/raw/main/"
        f"florr_mob_detector_training_package.tar.gz",
        f"https://raw.githubusercontent.com/{repo}/main/"
        f"florr_mob_detector_training_package.tar.gz",
        f"https://cdn.jsdelivr.net/gh/{repo}@main/"
        f"florr_mob_detector_training_package.tar.gz",
    ]
    tmp = "/tmp/_at_pkg.tar.gz"
    got = False
    for url in sources:
        for attempt in range(2):
            try:
                print(f"    尝试 {url.split('/')[2]} …")
                r = requests.get(url, headers=UA, timeout=(15, 300),
                                 stream=True)
                if r.status_code != 200:
                    print(f"    HTTP {r.status_code}")
                    break
                total = 0
                with open(tmp, "wb") as f:
                    for chunk in r.iter_content(1 << 20):
                        f.write(chunk)
                        total += len(chunk)
                if total > 1_000_000:
                    print(f"    ✓ 下载 {total // 1024 // 1024}MB")
                    got = True
                    break
            except Exception as e:
                print(f"    {type(e).__name__}")
                time.sleep(3)
        if got:
            break
    if not got:
        print("    全部源都失败")
        return False

    work = "/tmp/_at_synth"
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work, exist_ok=True)
    try:
        subprocess.run(["tar", "xzf", tmp, "-C", work], check=True,
                       timeout=300, capture_output=True)
    except Exception:
        return False

    mob_dir = os.path.join(work, "upload_package", "mob_images")
    bg_dir = os.path.join(work, "upload_package", "backgrounds")
    if not (os.path.isdir(mob_dir) and os.path.isdir(bg_dir)):
        print("    素材目录结构不符预期")
        return False

    # 合成器脚本位置和类名在这个仓库里都不规矩（目录名带 `~`，
    # 类名从 SyntheticDataGenerator 变成 HighQualityDataGenerator），所以按目录
    # 扫描 + 探测类名，不要写死路径。
    os.makedirs(dest, exist_ok=True)
    gen_scripts = []
    for root, _dirs, files in os.walk(work):
        for fn in files:
            if fn.endswith(".py") and "generate" in fn and "data" in fn:
                gen_scripts.append(os.path.join(root, fn))
    if not gen_scripts:
        print("    包里没找到数据生成脚本")
        return False
    print(f"    合成器: {os.path.relpath(gen_scripts[0], work)}")

    runner = f"""
import sys, importlib.util, inspect
spec = importlib.util.spec_from_file_location('gen', {gen_scripts[0]!r})
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
cls = None
for name, obj in vars(m).items():
    if inspect.isclass(obj) and hasattr(obj, 'generate_dataset'):
        cls = obj; break
if cls is None:
    print('no generator class'); sys.exit(1)
g = cls({mob_dir!r}, {bg_dir!r}, {dest!r})

# 这个仓库里合成器的类名和参数名改过好几版
# (SyntheticDataGenerator 用 samples_per_bg，HighQualityDataGenerator 用
#  total_samples)，所以按签名挑参数，不写死。
sig = inspect.signature(cls.generate_dataset)
kw = {{}}
want = {{
    'samples_per_bg': 6, 'total_samples': 150,
    'train_ratio': 0.8, 'num_mobs_range': (2, 8), 'scale_range': (0.1, 0.5),
}}
for k, v in want.items():
    if k in sig.parameters:
        kw[k] = v
print('using params:', list(kw))
g.generate_dataset(**kw)
"""
    try:
        p = subprocess.run([sys.executable, "-c", runner],
                           timeout=1200, capture_output=True, text=True)
        if p.returncode != 0:
            print(f"    合成失败: {(p.stderr or p.stdout)[-200:]}")
            return False
    except Exception as e:
        print(f"    合成异常: {type(e).__name__}: {str(e)[:150]}")
        return False
    return os.path.exists(os.path.join(dest, "data.yaml"))


# ---------------------------------------------------------------------------
# 3. 训练
# ---------------------------------------------------------------------------
def train(game: str, data_dir: str, epochs: int, imgsz: int, batch: int) -> dict:
    print(f"\n[3/4] 训练（epochs={epochs}, imgsz={imgsz}, batch={batch}）")
    try:
        import torch
    except Exception as e:
        return {"error": f"PyTorch 未安装: {e}"}

    cpu_only = os.environ.get("FORCE_CPU") == "1"
    device = "cpu" if cpu_only else ("0" if torch.cuda.is_available() else "cpu")
    print(f"  设备: {device}"
          + ("（未检测到 GPU，用 CPU；本机若无 AVX 会失败）" if device == "cpu" else ""))

    try:
        from ultralytics import YOLO
    except Exception as e:
        return {"error": f"ultralytics 未安装: {e}（pip install ultralytics）"}

    os.makedirs(MODELS_DIR, exist_ok=True)
    os.makedirs(LOG_DIR, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    proj = os.path.join(BASE, "training_runs", slug(game))

    try:
        YOLO("yolov8n.pt").train(
            data=os.path.join(data_dir, "data.yaml"),
            epochs=epochs, imgsz=imgsz, batch=batch, device=device,
            project=proj, name=stamp, exist_ok=True,
        )
    except Exception as e:
        log = os.path.join(LOG_DIR, f"{slug(game)}-{stamp}.log")
        return {"error": f"训练失败: {type(e).__name__}: {str(e)[:300]}",
                "log": log}

    best = os.path.join(proj, stamp, "weights", "best.pt")
    if not os.path.exists(best):
        return {"error": "训练结束但没找到 best.pt"}
    target = os.path.join(MODELS_DIR, f"{slug(game)}_detector.pt")
    shutil.copy2(best, target)
    return {"ok": True, "model": target,
            "size": os.path.getsize(target)}


# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="传游戏名，自动搜资料+备数据+训练")
    ap.add_argument("game", help="游戏名，如 florr.io")
    ap.add_argument("--epochs", type=int, default=30)
    ap.add_argument("--imgsz", type=int, default=640)
    ap.add_argument("--batch", type=int, default=16)
    ap.add_argument("--search-only", action="store_true")
    ap.add_argument("--skip-search", action="store_true")
    a = ap.parse_args()

    os.makedirs(DATA_ROOT, exist_ok=True)
    os.makedirs(MODELS_DIR, exist_ok=True)

    info = (search(a.game) if not a.skip_search
            else {"game": a.game, "refs": [], "entities": []})
    if a.search_only:
        print(json.dumps(info, ensure_ascii=False, indent=2))
        return

    data = resolve_data(a.game, info)
    if data.get("error"):
        print(f"\n✗ 中止：{data['error']}")
        print("  没有数据就不训 —— 产出一个假的模型比报错更糟。")
        sys.exit(2)

    r = train(a.game, data["dir"], a.epochs, a.imgsz, a.batch)
    if r.get("error"):
        print(f"\n✗ {r['error']}")
        sys.exit(3)

    print(f"\n✓ 完成: {r['model']}  ({r['size']} 字节)")
    print(f"  放进 perception 层：models/ 下已有同名文件，重启 perception_server 即生效")


if __name__ == "__main__":
    main()
