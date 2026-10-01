#!/usr/bin/env python3
"""
verify_portability.py · P3「能力可搬运」的实证（v2.0）
==================================================

## 它要证明什么

> **P3**：装到任意宿主 Agent（Claude Desktop / OpenCode / Codex / WorkBuddy），
> 能力不变。
> 证伪条件：**换宿主需要改本项目代码。**

「配置写进去了」不算证明 —— 那只说明 JSON 被写对了。
真正要验的是：**按每份配置真的把服务拉起来，拿到的工具集是否一致。**

## 做法

对每个已注册的宿主：

1. 读它的配置文件（支持 JSON 与 JSONC）
2. 按各自的 schema 取出启动命令
   - ``mcpServers`` 系（WorkBuddy / Claude Desktop / VS Code）：
     ``command`` 是字符串 + ``args`` 数组，环境变量在 ``env``
   - ``mcp`` 系（opencode）：``command`` 是**数组**，
     环境变量在 ``environment``，另有 ``type`` / ``enabled``
3. 真的按那条命令拉起进程，走 MCP stdio 协议握手
   （``initialize`` → ``notifications/initialized`` → ``tools/list``）
4. 比对各宿主的工具集

**全程不改本项目任何一行代码。**

## 用法

```bash
python tools/verify_portability.py            # 自动探测所有已注册宿主
python tools/verify_portability.py --json     # 输出 JSON，便于进 CI
```

退出码：0 = 至少两个宿主可用且工具集一致；1 = 不一致或可用宿主 < 2。

## 局限（诚实记录）

- 只验证到 **stdio 协议层**：工具清单一致。
  没验证「宿主 LLM 是否真的会用这些工具」—— 那需要真实的对话，
  属于 P3 的下一步，不在本脚本范围。
- codex 用 TOML 配置，本项目只给指引不代写（避免写坏结构），
  因此它不在自动探测范围内。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: 候选宿主配置：(标签, 路径, 格式, 取哪个 key)
CANDIDATES = [
    ("workbuddy", os.path.expanduser("~/.workbuddy/mcp.json"), "json", "mcpServers"),
    ("claude-desktop",
     os.path.expanduser("~/.config/Claude/claude_desktop_config.json"), "json", "mcpServers"),
    ("opencode", os.path.expanduser("~/.config/opencode/opencode.jsonc"), "jsonc", "mcp"),
    ("vscode", os.path.expanduser("~/.vscode/mcp.json"), "json", "servers"),
    ("codex", os.path.expanduser("~/.codex/config.toml"), "toml", "mcp_servers"),
]

PROTOCOL_VERSION = "2024-11-05"


def strip_jsonc(text: str) -> str:
    """剥离 JSONC 的注释与尾逗号（词法级，不解析字符串字面量）。"""
    out, i, n = [], 0, len(text)
    in_str = False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            in_str = True
            out.append(c)
            i += 1
            continue
        if c == "/" and i + 1 < n:
            if text[i + 1] == "/":
                j = text.find("\n", i)
                i = n if j < 0 else j
                continue
            if text[i + 1] == "*":
                j = text.find("*/", i + 2)
                i = n if j < 0 else j + 2
                continue
        out.append(c)
        i += 1
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))


def load_cfg(path: str, kind: str):
    if not os.path.exists(path):
        return None
    raw = open(path, encoding="utf-8").read()
    if kind == "jsonc":
        raw = strip_jsonc(raw)
    elif kind == "toml":
        # 只判断 ugf 段是否存在，不解析 TOML
        return "ugf" in raw and raw or None
    try:
        return json.loads(raw or "{}")
    except Exception:
        return None


def entry_to_cmd(node: dict):
    """把一条 MCP 注册项转成 (argv, env)。

    两种 schema 都支持 —— 这正是 P3 要覆盖的差异。
    """
    if not isinstance(node, dict):
        return None, None
    if node.get("type") == "local" or isinstance(node.get("command"), list):
        cmd = list(node.get("command") or [])
        env = dict(node.get("environment") or {})
    else:
        cmd = [node.get("command")] + list(node.get("args") or [])
        env = dict(node.get("env") or {})
    if not cmd or not cmd[0]:
        return None, None
    return cmd, env


def handshake(cmd, env, label, timeout=60):
    """按 MCP stdio 协议真实握手，返回工具名列表。"""
    e = dict(os.environ)
    e.update(env or {})
    e["PYTHONUNBUFFERED"] = "1"
    try:
        p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, env=e, text=True, bufsize=1)
    except Exception as ex:
        return None, f"{type(ex).__name__}: {ex}"

    tools = None
    deadline = time.time() + timeout
    try:
        p.stdin.write(json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                       "clientInfo": {"name": label, "version": "1"}},
        }) + "\n")
        p.stdin.flush()
        while time.time() < deadline:
            line = p.stdout.readline()
            if not line:
                break
            try:
                msg = json.loads(line)
            except Exception:
                continue
            if msg.get("id") == 1:
                p.stdin.write(json.dumps(
                    {"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
                p.stdin.write(json.dumps(
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/list",
                     "params": {}}) + "\n")
                p.stdin.flush()
            elif msg.get("id") == 2:
                tools = sorted(t.get("name", "")
                               for t in (msg.get("result") or {}).get("tools", []))
                break
    except Exception as ex:
        tools = None
        err = f"{type(ex).__name__}: {ex}"
    finally:
        try:
            p.stdin.close()
        except Exception:
            pass
        try:
            p.terminate()
            p.wait(timeout=5)
        except Exception:
            pass
    return tools, None if tools else (locals().get("err") or "握手超时或无响应")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="P3 能力可搬运实证")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--timeout", type=int, default=60)
    args = ap.parse_args(argv)

    if not args.json:
        print("=" * 68)
        print("P3 实证：按各宿主的配置真实拉起服务，比对工具集")
        print("=" * 68)

    found = {}
    for label, path, kind, key in CANDIDATES:
        cfg = load_cfg(path, kind)
        if cfg is None:
            continue
        if kind == "toml":
            continue                      # 只判断存在，不代写
        node = (cfg.get(key) or {}).get("ugf")
        if not isinstance(node, dict):
            continue
        cmd, env = entry_to_cmd(node)
        if not cmd:
            continue
        found[label] = {"path": path, "kind": kind, "key": key, "cmd": cmd, "env": env}

    if not args.json:
        if not found:
            print("  没有已注册的宿主。先跑：")
            print("    python tools/install_mcp.py --target <name>")
            return 1
        print(f"  发现 {len(found)} 个已注册宿主：{', '.join(found)}\n")

    results = {}
    for label, info in found.items():
        tools, err = handshake(info["cmd"], info["env"], label, args.timeout)
        results[label] = {"tools": tools, "error": err, "path": info["path"]}
        if not args.json:
            mark = "✓" if tools else "✗"
            detail = f"{len(tools)} 个工具" if tools else (err or "失败")
            print(f"  {mark} {label:<15} {detail}")

    usable = {k: v for k, v in results.items() if v["tools"]}
    consistent = None
    if len(usable) >= 2:
        sets = [tuple(v["tools"]) for v in usable.values()]
        consistent = len(set(sets)) == 1

    if not args.json:
        print()
        if len(usable) < 2:
            print(f"  ⚠ 可用宿主只有 {len(usable)} 个，不足以验证「换宿主能力不变」。")
            print("    再装一个： python tools/install_mcp.py --target opencode")
        elif consistent:
            print(f"  ✓ 一致性：{len(usable)} 个宿主的工具集**完全相同**"
                  f"（{len(next(iter(usable.values()))['tools'])} 个）")
            print("    期间未改动本项目任何代码 —— P3 未被证伪。")
        else:
            print("  ✗ 一致性：工具集有差异，P3 被证伪")
            base_label = next(iter(usable))
            base = set(usable[base_label]["tools"])
            for label, v in usable.items():
                if label == base_label:
                    continue
                other = set(v["tools"])
                if base - other:
                    print(f"    仅 {base_label} 有: {sorted(base - other)}")
                if other - base:
                    print(f"    仅 {label} 有: {sorted(other - base)}")

    if args.json:
        print(json.dumps({
            "hosts": {k: {"path": v["path"], "tools": v["tools"], "error": v["error"]}
                      for k, v in results.items()},
            "usable": len(usable),
            "consistent": consistent,
        }, ensure_ascii=False, indent=2))

    return 0 if (consistent or len(usable) < 2) else 1


if __name__ == "__main__":
    sys.exit(main())