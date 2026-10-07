#!/usr/bin/env python3
"""UGF 构建工具（ROADMAP #22）。

用法：
    python build.py pyz      # 构建 dist/ugf.pyz（zipapp 单文件，Python 3.10+ 直接运行）
    python build.py check    # 构建并冒烟自检（--version + selftest 19/19）

产物 dist/ugf.pyz 只含 agent.py + 入口壳，零第三方依赖即可跑通离线链路；
运行期数据（知识库/日志/状态）自动落 UGF_HOME（默认 ~/.ugf），见 ROADMAP #21。
"""

import os
import shutil
import subprocess
import sys
import zipapp

ROOT = os.path.dirname(os.path.abspath(__file__))
DIST = os.path.join(ROOT, "dist")
MAIN_SRC = '''"""ugf.pyz 入口壳：转发到 agent.main()。"""
import sys

import agent

if __name__ == "__main__":
    sys.exit(agent.main())
'''


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
            [sys.executable, pyz, "selftest"], capture_output=True, text=True, env=env, timeout=300
        )
        if s.returncode != 0 or "19/19" not in s.stdout:
            print(f"✗ selftest 失败:\n{s.stdout[-800:]}")
            return 1
        print("  selftest 19/19 ✓")
    return 0


def main(argv=None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    cmd = args[0] if args else "pyz"
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
