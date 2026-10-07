#!/usr/bin/env python3
"""UGF 构建工具（ROADMAP #22 zipapp / ROADMAP #2 模块化分片）。

用法：
    python build.py pyz           # 构建 dist/ugf.pyz（zipapp 单文件，Python 3.10+ 直接运行）
    python build.py check         # 构建并冒烟自检（--version + selftest 19/19）
    python build.py split         # 把 agent.py 按横幅注释拆分为 src/ugf/ 分片（开发态源）
    python build.py agent         # 从 src/ugf/ 分片合并构建 agent.py（发布态单文件）
    python build.py check-agent   # 校验分片构建产物与仓库内 agent.py 逐字节一致（CI 用）

双源工作流（ROADMAP #2）：
    - 日常开发改 src/ugf/ 分片 → python build.py agent 重新生成单文件
    - 应急直接改 agent.py → python build.py split 反向同步分片
    - CI 的 check-agent 保证两个方向都不会漂移

产物 dist/ugf.pyz 只含 agent.py + 入口壳，零第三方依赖即可跑通离线链路；
运行期数据（知识库/日志/状态）自动落 UGF_HOME（默认 ~/.ugf），见 ROADMAP #21。
"""

import os
import re
import shutil
import subprocess
import sys
import zipapp

ROOT = os.path.dirname(os.path.abspath(__file__))
DIST = os.path.join(ROOT, "dist")
PARTS_DIR = os.path.join(ROOT, "src", "ugf")

BANNER_RE = re.compile(r"(?m)^# ={10,}\n# (\d+a?)\. ([^\n]+)\n# ={10,}\n")
SLUGS = {
    "0": "utils",
    "1": "config",
    "2": "kb",
    "3": "predict",
    "4": "combat",
    "5": "knowledge",
    "6": "perception",
    "7": "action",
    "8": "session",
    "9": "review",
    "10": "report",
    "11": "tune",
    "12": "learn",
    "13": "llm",
    "14": "loop",
    "14a": "panel",
    "15": "cli",
    "15a": "profile_check",
}
MAIN_SRC = '''"""ugf.pyz 入口壳：转发到 agent.main()。"""
import sys

import agent

if __name__ == "__main__":
    sys.exit(agent.main())
'''


# ---------------------------------------------------------------------------
# ROADMAP #22：zipapp
# ---------------------------------------------------------------------------
def build_pyz() -> str:
    staging = os.path.join(DIST, "_pyz_stage")
    if os.path.isdir(staging):
        shutil.rmtree(staging)
    os.makedirs(staging)
    shutil.copy2(os.path.join(ROOT, "agent.py"), os.path.join(staging, "agent.py"))
    with open(os.path.join(staging, "__main__.py"), "w", encoding="utf-8") as f:
        f.write(MAIN_SRC)
    out = os.path.join(DIST, "ugf.pyz")
    zipapp.create_archive(staging, target=out, interpreter="/usr/bin/env python3")
    shutil.rmtree(staging)
    return out


def smoke_test(pyz: str) -> int:
    import tempfile

    with tempfile.TemporaryDirectory() as home:
        env = dict(os.environ, UGF_HOME=home, UGF_DRY_RUN="1", UGF_PERCEPTION_BACKEND="mock")
        v = subprocess.run([sys.executable, pyz, "--version"], capture_output=True, text=True, env=env)
        if v.returncode != 0:
            print(f"✗ --version 失败: {v.stderr}")
            return 1
        print(f"  {v.stdout.strip()}")
        s = subprocess.run(
            [sys.executable, pyz, "selftest"],
            capture_output=True,
            text=True,
            env=env,
            timeout=300,
        )
        if s.returncode != 0 or "19/19" not in s.stdout:
            print(f"✗ selftest 失败:\n{s.stdout[-800:]}")
            return 1
        print("  selftest 19/19 ✓")
    return 0


# ---------------------------------------------------------------------------
# ROADMAP #2：模块化分片 ↔ 单文件互转
# ---------------------------------------------------------------------------
def _concat_parts() -> str:
    parts = sorted(f for f in os.listdir(PARTS_DIR) if f.endswith(".py"))
    chunks = []
    for fn in parts:
        with open(os.path.join(PARTS_DIR, fn), encoding="utf-8") as f:
            chunks.append(f.read())
    return "".join(chunks)


def split_agent() -> int:
    """把 agent.py 按横幅注释拆分为 src/ugf/ 分片（内容零改动，拼接可逐字节还原）。"""
    with open(os.path.join(ROOT, "agent.py"), encoding="utf-8") as f:
        src = f.read()
    marks = list(BANNER_RE.finditer(src))
    if not marks:
        print("✗ 未找到任何段落横幅，agent.py 结构异常")
        return 1
    os.makedirs(PARTS_DIR, exist_ok=True)
    for fn in os.listdir(PARTS_DIR):
        if fn.endswith(".py"):
            os.remove(os.path.join(PARTS_DIR, fn))
    bounds = [m.start() for m in marks] + [len(src)]
    with open(os.path.join(PARTS_DIR, "part00_header.py"), "w", encoding="utf-8") as f:
        f.write(src[: bounds[0]])
    for i, m in enumerate(marks):
        num = m.group(1)
        slug = SLUGS.get(num, f"sec{num}")
        name = f"part{i + 1:02d}_{num}_{slug}.py"
        with open(os.path.join(PARTS_DIR, name), "w", encoding="utf-8") as f:
            f.write(src[bounds[i] : bounds[i + 1]])
    if _concat_parts() != src:
        print("✗ 拆分后拼接与原文不一致，请检查（分片已写入，可 git checkout 恢复）")
        return 1
    print(f"✓ 已拆分为 {len(marks) + 1} 个分片（src/ugf/），拼接校验逐字节一致")
    return 0


def build_agent(check_only: bool = False) -> int:
    """从 src/ugf/ 分片合并构建 agent.py；check_only 时只比对不落盘。"""
    if not os.path.isdir(PARTS_DIR) or not any(f.endswith(".py") for f in os.listdir(PARTS_DIR)):
        print("✗ src/ugf/ 无分片文件，先执行 python build.py split")
        return 1
    built = _concat_parts()
    target = os.path.join(ROOT, "agent.py")
    if check_only:
        with open(target, encoding="utf-8") as f:
            current = f.read()
        if current != built:
            print("✗ agent.py 与分片构建不一致！")
            print("  改过分片 → 运行 python build.py agent 重新生成；")
            print("  直接改过 agent.py → 运行 python build.py split 反向同步。")
            return 1
        n = len([f for f in os.listdir(PARTS_DIR) if f.endswith(".py")])
        print(f"✓ 分片构建与 agent.py 逐字节一致（{n} 个分片）")
        return 0
    with open(target, "w", encoding="utf-8") as f:
        f.write(built)
    print(f"✓ 已从分片构建 agent.py（{len(built.encode('utf-8')) / 1024:.0f} KB）")
    return 0


def main(argv=None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    cmd = args[0] if args else "pyz"
    if cmd == "split":
        return split_agent()
    if cmd == "agent":
        return build_agent(check_only=False)
    if cmd == "check-agent":
        return build_agent(check_only=True)
    if cmd not in ("pyz", "check"):
        print(__doc__)
        return 2
    out = build_pyz()
    print(f"✓ 已构建 {out}（{os.path.getsize(out) / 1024:.0f} KB）")
    print(f"  运行: UGF_HOME=~/.ugf python {out} selftest")
    if cmd == "check":
        print("冒烟自检...")
        return smoke_test(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
