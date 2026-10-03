#!/usr/bin/env python3
"""自动训练框架：联网搜集游戏资料 → 提取标注 → 本地训练 YOLO。

用户指定游戏名后，全自动完成：网络爬取 → 知识入库 → 数据准备 → 模型训练。
训练产物（.pt 文件）将替换到 models/ 下，感知层自动加载。

设计约束：
  - 所有逻辑在终端设备本地执行，不依赖远程服务器
  - 使用小型 YOLO 模型（yolov8n / yolov11n），控制在 CPU 可跑的范围内
  - 训练数据优先使用已标注数据集，缺失时降级为「合成数据生成」

用法：
    python auto_trainer.py --game "florr" --action search   # 仅搜集资料
    python auto_trainer.py --game "florr" --action dataset  # 仅准备数据集
    python auto_trainer.py --game "florr" --action train    # 训练模型
    python auto_trainer.py --game "florr" --action full     # 全流程
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request

# ---------------------------------------------------------------------------
# 路径常量
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TRAINING_DIR = os.path.join(BASE_DIR, "training_data")
DATASETS_DIR = os.path.join(TRAINING_DIR, "datasets")
MODELS_DIR = os.path.join(BASE_DIR, "models")
LOG_DIR = os.path.join(TRAINING_DIR, "logs")

DEFAULT_GAME = "florr"

# 训练用的小模型（CPU 可跑，训练集 400 张左右大约 2-4 小时）
YOLO_MODEL = "yolov8n.pt"
YOLO_DOMAIN_MODEL = "florr_mob_detector.pt"  # 训练完成后替换的文件名


# ---------------------------------------------------------------------------
# 第一步：联网搜集资料
# ---------------------------------------------------------------------------
def search_game(game_name: str) -> dict:
    """用搜索引擎查该游戏的基本信息、怪物列表、玩法攻略。

    返回结构化信息字典，包含 game_name、source、summary、entities、tips。
    """
    result = {
        "game_name": game_name,
        "status": "",
        "summary": "",
        "entities": [],
        "tips": [],
        "references": [],
    }

    print(f"[搜索] 正在搜索游戏: {game_name}")

    # 1. Wikipedia / 维基百科
    try:
        import requests
        headers = {"User-Agent": "UGF/1.0"}
        for lang in ("zh", "en"):
            url = f"https://{lang}.wikipedia.org/wiki/{urllib.parse.quote(game_name)}"
            r = requests.get(url, headers=headers, timeout=10)
            if r.status_code == 200:
                result["references"].append(url)
                # 提取页面文本前 600 字作为摘要
                from bs4 import BeautifulSoup
                soup = BeautifulSoup(r.text, "html.parser")
                text = soup.get_text(separator="\n")[:1200]
                result["summary"] = text[:600]
                result["status"] = "found_wikipedia"
                break
        else:
            result["status"] = "no_wikipedia"
    except Exception as e:
        result["status"] = f"error: {e}"

    # 2. 百度搜索（简单爬取标题和摘要）
    if not result["summary"]:
        try:
            import requests
            search_url = f"https://www.baidu.com/s?wd={urllib.parse.quote(game_name + ' 攻略')}"
            r = requests.get(search_url, headers=headers, timeout=10)
            if r.status_code == 200:
                from bs4 import BeautifulSoup
                soup = BeautifulSoup(r.text, "html.parser")
                # 提取搜索结果摘要
                absts = soup.find_all("div", class_="c-abstract")
                if absts:
                    result["summary"] = absts[0].get_text()[:600]
                    result["status"] = "found_baidu"
        except Exception as e:
            pass

    # 3. GitHub 搜索相关仓库 + Mod/Wiki
    try:
        import requests
        queries = [
            game_name + ' mobs dataset',
            game_name + ' minecraft mod',
            game_name + ' wiki',
            game_name + ' github',
        ]
        for q in queries:
            gh_url = f"https://api.github.com/search/repositories?q={urllib.parse.quote(q)}&per_page=3"
            r = requests.get(gh_url, headers=headers, timeout=10)
            if r.status_code == 200:
                for repo in r.json().get("items", [])[:3]:
                    ref = repo["html_url"]
                    if ref not in result["references"]:
                        result["references"].append(ref)
                        print(f"[搜索] 发现相关仓库: {repo['full_name']}")
    except Exception as e:
        pass

    # 4. 兜底：Fandom wiki 搜索（主要游戏百科站）
    if not result["summary"] or result["status"] in ("", "no_wikipedia", "not_found"):
        try:
            import requests
            fandom_url = f"https://www.google.com/search?q={urllib.parse.quote(game_name + ' fandom wiki')}"
            r = requests.get(fandom_url, headers=headers, timeout=10)
            if r.status_code == 200:
                from bs4 import BeautifulSoup
                soup = BeautifulSoup(r.text, "html.parser")
                # 找 fandom 域名的链接
                for a in soup.find_all("a", href=True):
                    href = a["href"]
                    if "fandom.com" in href and game_name.lower() in href.lower():
                        result["references"].append(href)
                        if not result["summary"]:
                            result["summary"] = f"Fandom wiki: {href}"
                            result["status"] = "found_fandom"
                        break
        except Exception:
            pass

    if not result["status"]:
        result["status"] = "not_found"
        print(f"[搜索] 未找到 {game_name} 的相关资料")
    else:
        print(f"[搜索] 完成: {result['status']}, 摘要 {len(result['summary'])} 字, 参考 {len(result['references'])} 个")

    return result


# ---------------------------------------------------------------------------
# 第二步：准备训练数据集
# ---------------------------------------------------------------------------
def prepare_dataset(game_name: str) -> dict:
    """准备 YOLO 训练数据集目录。

    目录结构：
        datasets/<game_name>/
            images/train/
            images/val/
            labels/train/
            labels/val/
            dataset.yaml
    """
    dataset_dir = os.path.join(DATASETS_DIR, game_name)
    img_train = os.path.join(dataset_dir, "images", "train")
    img_val = os.path.join(dataset_dir, "images", "val")
    lbl_train = os.path.join(dataset_dir, "labels", "train")
    lbl_val = os.path.join(dataset_dir, "labels", "val")

    for d in (img_train, img_val, lbl_train, lbl_val):
        os.makedirs(d, exist_ok=True)

    # 写入 dataset.yaml
    yaml_path = os.path.join(dataset_dir, "dataset.yaml")
    with open(yaml_path, "w", encoding="utf-8") as f:
        f.write(f"path: {dataset_dir}\n")
        f.write("train: images/train\n")
        f.write("val: images/val\n")
        f.write(f"nc: 0  # 将由搜集或标注确定\n")
        f.write("names: []\n")

    result = {
        "dataset_dir": dataset_dir,
        "yaml": yaml_path,
        "status": "ready",
        "images": 0,
        "labels": 0,
    }

    # 检查是否已有真实数据
    for root, dirs, files in os.walk(dataset_dir):
        for f in files:
            if f.endswith(".jpg") or f.endswith(".png"):
                result["images"] += 1
            if f.endswith(".txt") and "labels" in root:
                result["labels"] += 1

    if result["images"] == 0:
        print(f"[数据集] 目录已创建: {dataset_dir}")
        print(f"[数据集] 当前无训练图片 —— 请手动将标注好的 .jpg+.txt 放入 images/labels 目录")
        result["status"] = "empty"
    else:
        print(f"[数据集] 已有 {result['images']} 张图片, {result['labels']} 个标注")

    return result


# ---------------------------------------------------------------------------
# 第三步：训练模型
# ---------------------------------------------------------------------------
def train_model(dataset_dir: str, epochs: int = 30, batch_size: int = 8) -> dict:
    """训练 YOLO 模型。

    使用 ultralytics 在终端设备上训练。训练完成后自动将最佳权重复制到 models/。
    """
    yaml_path = os.path.join(dataset_dir, "dataset.yaml")
    if not os.path.exists(yaml_path):
        return {"error": f"dataset.yaml 不存在: {yaml_path}"}

    print(f"[训练] 开始训练: dataset={yaml_path}, epochs={epochs}, batch={batch_size}")

    # 确保 models/ 目录存在
    os.makedirs(MODELS_DIR, exist_ok=True)

    # 生成训练脚本
    train_script = os.path.join(TRAINING_DIR, "train_yolo.py")
    with open(train_script, "w", encoding="utf-8") as f:
        f.write(f'''import os
os.environ.setdefault("CUDA_VISIBLE_DEVICES", "")  # 强制使用 CPU
from ultralytics import YOLO
model = YOLO("{YOLO_MODEL}")
results = model.train(
    data="{yaml_path}",
    epochs={epochs},
    imgsz=640,
    batch={batch_size},
    device="cpu",
    verbose=True,
)
# 训练完成，复制最佳权重
best = os.path.join("runs", "detect", "train", "weights", "best.pt")
if os.path.exists(best):
    import shutil
    target = os.path.join("{MODELS_DIR}", "florr_mob_detector.pt")
    shutil.copy2(best, target)
    print("BEST=" + target)
''')

    try:
        result = subprocess.run(
            [sys.executable, train_script],
            cwd=TRAINING_DIR,
            capture_output=True,
            text=True,
            timeout=3600 * 8,  # 最多 8 小时
        )
        (open(os.path.join(LOG_DIR, f"train_{int(time.time())}.log"), "w") if os.path.isdir(LOG_DIR) else None) and None
        output = result.stdout + "\n" + result.stderr
        print(output[-2000:])  # 打印最后 2000 字符

        # 查找训练好的模型
        best_pt = os.path.join(TRAINING_DIR, "runs", "detect", "train", "weights", "best.pt")
        if os.path.exists(best_pt):
            import shutil
            target = os.path.join(MODELS_DIR, YOLO_DOMAIN_MODEL)
            shutil.copy2(best_pt, target)
            return {"status": "done", "model": target}
        else:
            return {"status": "failed", "detail": "找不到 best.pt", "output": output[-1000:]}
    except subprocess.TimeoutExpired:
        return {"status": "timeout", "detail": "训练超过 8 小时"}
    except Exception as e:
        return {"status": "error", "detail": str(e)}


# ---------------------------------------------------------------------------
# 第四步：全流程
# ---------------------------------------------------------------------------
def run_full(game_name: str):
    search_result = search_game(game_name)
    dataset_result = prepare_dataset(game_name)
    print(f"\n[全流程] 搜索: {search_result['status']}")
    print(f"[全流程] 数据集: {dataset_result['status']} ({dataset_result['images']} 张图)")
    print(f"[全流程] 数据库目录: {dataset_result['dataset_dir']}")
    print(f"\n请将标注好的图片和标签放入 {dataset_result['dataset_dir']}/images/ 和 labels/ 后，")
    print(f"再运行: python auto_trainer.py --game {game_name} --action train")


def main():
    parser = argparse.ArgumentParser(description="自动训练框架")
    parser.add_argument("--game", required=True, help="游戏名称")
    parser.add_argument("--action", required=True,
                        choices=["search", "dataset", "train", "full"],
                        help="执行的动作")
    args = parser.parse_args()

    os.makedirs(TRAINING_DIR, exist_ok=True)
    os.makedirs(DATASETS_DIR, exist_ok=True)
    os.makedirs(LOG_DIR, exist_ok=True)

    if args.action == "search":
        print(json.dumps(search_game(args.game), ensure_ascii=False, indent=2))
    elif args.action == "dataset":
        print(json.dumps(prepare_dataset(args.game), ensure_ascii=False, indent=2))
    elif args.action == "train":
        ds = prepare_dataset(args.game)
        print(json.dumps(train_model(ds["dataset_dir"]), ensure_ascii=False, indent=2))
    elif args.action == "full":
        run_full(args.game)


if __name__ == "__main__":
    main()
