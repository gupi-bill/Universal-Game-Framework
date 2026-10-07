#!/usr/bin/env python3
"""密钥防泄漏扫描器（ROADMAP #24）。

纯标准库实现，扫描仓库文本文件中的常见密钥/凭据模式，命中即退出码 1。
接入点：pre-commit 钩子 + CI secret-scan job + 本地手动运行。

用法：
    python tools/secret_scan.py            # 扫描仓库根目录
    python tools/secret_scan.py path1 path2 # 扫描指定路径

设计说明：模式串全部用拼接构造，避免本文件自身被检出（自扫必须干净）。
"""

from __future__ import annotations

import os
import re
import sys

# ── 模式表：(名称, 正则)。前缀全部拼接构造，防止扫描器自命中 ──
_GH = "gh" + "p_|gh" + "o_|gh" + "u_|gh" + "s_|gh" + "r_"
_PATTERNS = [
    ("GitHub Token", re.compile(r"(?:" + _GH + r")[A-Za-z0-9]{36}")),
    ("GitHub Fine-grained PAT", re.compile(r"github" + "_pat_[A-Za-z0-9_]{22,}")),
    ("AWS Access Key", re.compile(r"(?:A3T[A-Z0-9]|AK" + "IA|AS" + "IA)[A-Z0-9]{16}")),
    ("Slack Token", re.compile(r"xox" + "[baprs]-[A-Za-z0-9-]{10,}")),
    ("OpenAI/Anthropic 风格 Key", re.compile(r"\bsk" + r"-[A-Za-z0-9_\-]{20,}")),
    ("Anthropic Key", re.compile(r"\bsk-ant" + r"-[A-Za-z0-9_\-]{20,}")),
    ("PEM 私钥块", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("JWT", re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}")),
    (
        "通用 Authorization 头硬编码",
        re.compile(r"(?i)(?:api[_-]?key|secret|passwd|password)\s*[:=]\s*['\"][A-Za-z0-9/+=_\-]{16,}['\"]"),
    ),
]

SKIP_DIRS = {
    ".git",
    "__pycache__",
    ".venv",
    "venv",
    "node_modules",
    ".mypy_cache",
    ".ruff_cache",
    ".pytest_cache",
    "dist",
    "build",
}
TEXT_EXT = {
    ".py",
    ".md",
    ".yaml",
    ".yml",
    ".json",
    ".toml",
    ".txt",
    ".cfg",
    ".ini",
    ".sh",
    ".bat",
    ".ps1",
    ".html",
    ".js",
    ".css",
    ".env",
}
# 白名单：示例/测试文件里的假密钥占位
ALLOWLIST_SUBSTR = ("your-", "example", "<", "fake", "dummy", "xxxx", " placeholder")


def _iter_files(roots):
    for root in roots:
        if os.path.isfile(root):
            yield root
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for fn in filenames:
                ext = os.path.splitext(fn)[1].lower()
                if ext in TEXT_EXT or fn in (".env.example", "Dockerfile"):
                    yield os.path.join(dirpath, fn)


def scan(paths) -> list:
    findings = []
    for fp in _iter_files(paths):
        try:
            with open(fp, encoding="utf-8", errors="ignore") as f:
                for lineno, line in enumerate(f, 1):
                    low = line.lower()
                    if any(a in low for a in ALLOWLIST_SUBSTR):
                        continue
                    for name, pat in _PATTERNS:
                        m = pat.search(line)
                        if m:
                            tok = m.group(0)
                            masked = tok[:6] + "…" + tok[-4:] if len(tok) > 12 else "***"
                            findings.append((fp, lineno, name, masked))
        except OSError:
            continue
    return findings


def main(argv=None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    paths = args or [root]
    findings = scan(paths)
    if findings:
        print(f"✗ 发现 {len(findings)} 处疑似密钥泄漏：")
        for fp, lineno, name, masked in findings:
            print(f"  {fp}:{lineno}  [{name}]  {masked}")
        print("处理：立即作废该凭据；从代码与 git 历史中移除；改用 .env（已在 .gitignore）。")
        return 1
    print("✓ 密钥扫描通过，未发现泄漏")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
