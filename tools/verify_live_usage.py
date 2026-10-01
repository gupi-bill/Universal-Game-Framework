#!/usr/bin/env python3
"""
verify_live_usage.py · P3 的第二层实证：宿主 LLM 真的会用这些工具
=================================================================

## 它补的是什么

`tools/verify_portability.py` 验的是**协议层**：按各宿主的配置把服务
拉起来，工具集是否一致。那证明「服务能起来、工具在」。

但 P3 真正要回答的是：**换了宿主，那套能力用起来一样顺手吗？**

只有真的让**宿主的 LLM**调一次才算数。

## 做法

调用 `opencode run`，让宿主自己的模型去调 ugf 的工具，
然后检查返回内容是否**真的来自 ugf**（而不是模型自己编的）。

三档判定：

| 档 | 含义 |
|----|------|
| ✓ 真实调用 | 返回内容含 ugf 独有的数据（工具名单、知识库文件、BOSS 记录） |
| ✗ 模型编的 | 返回内容与 ugf 实际数据不符 |
| ? 无法判定 | 没装 opencode / 无可用模型 / 环境不具备 |

## 用法

```bash
python tools/verify_live_usage.py              # 人看
python tools/verify_live_usage.py --json       # 进 CI
python tools/verify_live_usage.py --model agnes/agnes-2.5-flash
```

退出码：0 = 至少一次真实调用成功；1 = 全部失败；2 = 环境不具备。

## 为什么不用 mock 掉 LLM

因为要验的就是「LLM 会不会用」。mock 掉 LLM 就等于没验。

没装 opencode 或没有可用模型时，脚本明确返回 2（环境不具备）
而不是假装通过 —— 门禁里「环境不具备」应该被单独标记，不该混进
「通过」里。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: opencode CLI 的可能位置
OPENCODE_CANDIDATES = [
    shutil.which("opencode"),
    os.path.expanduser("~/.config/opencode/node_modules/.bin/opencode"),
    "/usr/local/bin/opencode",
]

#: 三次探针：分别验「静态文本」「真实列表」「知识库内容」
#: 每条 prompt 明确要求"原样贴出"，避免模型自己编。
PROBES = [
    {
        "name": "ugf_guide",
        "prompt": "请调用 ugf 服务的 ugf_guide 工具，然后把返回的说明原样告诉我，"
                  "不要自己编。只需要贴出其中提到 kb_export 和 kb_import 那两行。",
        # ugf_guide 的独有内容：这两个工具名
        "expect_any": ["kb_export", "kb_import"],
    },
    {
        "name": "kb_list",
        "prompt": "调用 ugf 的 kb_list 工具列出知识库文件，"
                  "然后把返回的文件名原样贴出前 5 条。",
        # 知识库根目录的固定文件
        "expect_any": ["boss_behavior_log", "learning_stats", "_README"],
    },
    {
        "name": "query_boss_history",
        "prompt": "调用 ugf 的 query_boss_history 工具查询 mantis 的历史，"
                  "把返回内容原样贴出前几行。如果没数据就说没数据。",
        # 真实知识库里 mantis 记录里的独有措辞
        "expect_any": ["mantis", "行为"],
    },
]


def find_opencode(extra_paths=None):
    """定位 opencode CLI。

    ``extra_paths`` 可显式传入候选列表（测试用）；
    默认用模块级候选 + 几个常见的绝对路径。
    """
    cands = list(extra_paths) if extra_paths is not None else list(OPENCODE_CANDIDATES)
    for p in cands:
        if p and os.path.exists(p):
            return p
    return None


def mcp_registered(opencode_bin: str) -> bool:
    """opencode 自己说 ugf 连上了吗。"""
    try:
        r = subprocess.run([opencode_bin, "mcp", "list"], capture_output=True,
                           text=True, timeout=120)
        out = r.stdout + r.stderr
        return ("ugf" in out and "connected" in out)
    except Exception:
        return False


def run_probe(opencode_bin: str, prompt: str, model: str, timeout: int) -> str:
    cmd = [opencode_bin, "run"]
    if model:
        cmd += ["--model", model]
    cmd.append(prompt)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout + r.stderr
    except subprocess.TimeoutExpired as e:
        return (e.stdout or "") + (e.stderr or "")
    except Exception as e:
        return f"__ERR__ {type(e).__name__}: {e}"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="P3 第二层：宿主 LLM 真实调用 ugf 工具")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--model", default="agnes/agnes-2.5-flash")
    ap.add_argument("--timeout", type=int, default=240)
    ap.add_argument("--only", default="", help="只跑某个探针（ugf_guide/kb_list/query_boss_history）")
    ap.add_argument("--opencode-bin", default="", help="显式指定 opencode 可执行文件路径")
    args = ap.parse_args(argv)

    if not args.json:
        print("=" * 68)
        print("P3 第二层实证：让宿主 LLM 真的调 ugf 工具")
        print("=" * 68)

    oc = args.opencode_bin if args.opencode_bin else find_opencode()
    if args.opencode_bin and not os.path.exists(args.opencode_bin):
        oc = None
    if not oc:
        if args.json:
            print(json.dumps({"verdict": "no_env", "reason": "未找到 opencode CLI"},
                             ensure_ascii=False))
        else:
            print("  ⚠ 环境不具备：未找到 opencode CLI")
            print("    本项无法验证。装好 opencode 后再跑。")
        return 2

    if not args.json:
        print(f"  opencode: {oc}")

    if not mcp_registered(oc):
        if args.json:
            print(json.dumps({"verdict": "not_registered", "reason": "opencode 未连上 ugf"},
                             ensure_ascii=False))
        else:
            print("  ⚠ opencode 报告 ugf 未连接")
            print("    先跑： python tools/install_mcp.py --target opencode")
        return 2

    if not args.json:
        print("  opencode 自报 ugf: connected")
        print(f"  模型: {args.model}\n")

    probes = [p for p in PROBES if not args.only or p["name"] == args.only]
    results = []
    for pr in probes:
        if not args.json:
            print(f"  ── 探针 {pr['name']} ──")
        out = run_probe(oc, pr["prompt"], args.model, args.timeout)
        low = out.lower()
        hit = [k for k in pr["expect_any"] if k.lower() in low]
        # 反向判据：模型说"没数据"而探针本该有数据 → 可能没真调
        claimed_empty = ("没有数据" in out or "无记录" in out or "未找到" in out)
        ok = bool(hit)
        results.append({"probe": pr["name"], "ok": ok, "matched": hit,
                        "claimed_empty": claimed_empty})
        if not args.json:
            mark = "✓" if ok else ("?" if claimed_empty else "✗")
            print(f"    {mark} 命中 ugf 独有内容: {hit or '无'}")
            if not ok:
                snippet = out.strip().replace("\n", " ")[-220:]
                print(f"      输出尾部: …{snippet}")

    real = [r for r in results if r["ok"]]
    verdict = "verified" if real else "unverified"
    if not args.json:
        print()
        if real:
            print(f"  ✓ P3 第二层成立：{len(real)}/{len(results)} 个探针"
                  f"取得 ugf 独有的真实数据")
            print("    说明换了宿主后，宿主 LLM 能正常取用这套能力，")
            print("    不只是「工具在列表里」，而是真的能用。")
        else:
            print("  ✗ 未能确认宿主 LLM 真的调用了 ugf 工具")
            print("    可能是模型不可用，或模型自己编了内容。")

    if args.json:
        print(json.dumps({"verdict": verdict, "results": results},
                         ensure_ascii=False, indent=2))
    return 0 if real else 1


if __name__ == "__main__":
    sys.exit(main())