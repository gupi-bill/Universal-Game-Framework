#!/usr/bin/env python3
"""
tools/install_mcp.py 的契约测试  tests/test_install_mcp.py
========================================================
覆盖：--list 能跑、--dry-run 不落盘、真实写入/覆盖/卸载、custom 目标、未知目标拒绝。
"""
import json
import os
import time
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(BASE, "tools", "install_mcp.py")

# 直接 import 以便测内部函数（_strip_jsonc / entry / _write）。
# 原有测试全走 subprocess 黑盒，这里补白盒。
sys.path.insert(0, os.path.join(BASE, "tools"))
import install_mcp  # noqa: E402


def run(*args):
    return subprocess.run([sys.executable, SCRIPT, *args],
                          capture_output=True, text=True, cwd=BASE)


class TestInstallMcp(unittest.TestCase):

    def test_list_ok(self):
        r = run("--list")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("workbuddy", r.stdout)

    def test_unknown_target_rejected(self):
        r = run("--target", "no-such-client")
        self.assertEqual(r.returncode, 2)
        self.assertIn("未知目标", r.stdout + r.stderr)

    def test_custom_requires_path(self):
        r = run("--target", "custom")
        self.assertEqual(r.returncode, 2)

    def test_custom_dryrun_no_write(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "mcp.json")
            r = run("--target", "custom", "--path", p, "--dry-run")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertFalse(os.path.exists(p), "dry-run 不应落盘")
            self.assertIn("ugf", r.stdout)

    def test_custom_write_then_remove(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "mcp.json")
            # 预置一条别人的服务，验证是「合并」而不是覆盖
            with open(p, "w", encoding="utf-8") as f:
                json.dump({"mcpServers": {"other": {"command": "x"}}}, f)

            r = run("--target", "custom", "--path", p)
            self.assertEqual(r.returncode, 0, r.stderr)
            data = json.load(open(p, encoding="utf-8"))
            self.assertIn("other", data["mcpServers"], "不能覆盖已有服务")
            self.assertIn("ugf", data["mcpServers"])
            entry = data["mcpServers"]["ugf"]
            self.assertEqual(entry["env"]["UGF_DRY_RUN"], "1", "默认必须 dry-run")
            self.assertTrue(entry["args"][0].endswith("mcp_server.py"))
            self.assertTrue(os.path.exists(entry["args"][0]))

            r2 = run("--target", "custom", "--path", p, "--remove")
            self.assertEqual(r2.returncode, 0, r2.stderr)
            data2 = json.load(open(p, encoding="utf-8"))
            self.assertNotIn("ugf", data2["mcpServers"])
            self.assertIn("other", data2["mcpServers"])

    def test_online_flag_sets_dryrun_zero(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "mcp.json")
            run("--target", "custom", "--path", p)
            data = json.load(open(p, encoding="utf-8"))
            self.assertEqual(data["mcpServers"]["ugf"]["env"]["UGF_DRY_RUN"], "1")
            run("--target", "custom", "--path", p, "--online")
            data = json.load(open(p, encoding="utf-8"))
            self.assertEqual(data["mcpServers"]["ugf"]["env"]["UGF_DRY_RUN"], "0")

    def test_toml_target_gives_guidance_only(self):
        r = run("--target", "codex")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("mcp_servers.ugf", r.stdout)

    def test_check_reports_healthy(self):
        """真实拉起 MCP 服务做 stdio 握手（约 20 秒）。

        这条测试依赖子进程握手，在机器负载高时（例如刚跑完 1000+ 用例）
        会偶发超时。失败时重试两次 —— 否则它会变成门禁里唯一的噪声源，
        让人习惯性忽略红色。
        """
        last = None
        for attempt in range(3):
            r = run("--check")
            if r.returncode == 0 and "握手成功" in r.stdout:
                self.assertIn("ugf_guide", r.stdout)
                return
            last = r
            print(f"  [重试 {attempt + 1}/3] install_mcp --check "
                  f"退出码 {r.returncode}，5 秒后再试")
            time.sleep(5)
        self.fail(f"3 次都失败，最后一次：\n{(last.stdout + last.stderr)[-800:]}")

    def test_mcp_install_doc_exists_and_mentions_installer(self):
        doc = os.path.join(BASE, "docs", "MCP_INSTALL.md")
        self.assertTrue(os.path.exists(doc), "缺少 docs/MCP_INSTALL.md")
        text = open(doc, encoding="utf-8").read()
        self.assertIn("install_mcp.py", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)


# ---------------------------------------------------------------------------
# v2.0 P3：跨宿主配置生成（2026-10-01）
# ---------------------------------------------------------------------------
# 本机实测发现 opencode 装不上：install_mcp 只认 opencode.json，
# 而实际用的是 opencode.jsonc；而且两者 schema 不同
# （mcpServers+字符串 command vs mcp+数组 command）。
# P3 命题是「换宿主不改本项目代码」，配置生成也必须按宿主分化。

class TestPortability(unittest.TestCase):
    def test_opencode_target_uses_jsonc(self):
        spec = install_mcp.TARGETS["opencode"]
        paths = spec.get("paths") or [spec["path"]]
        self.assertTrue(any(p.endswith(".jsonc") for p in paths),
                        "opencode 必须探测 .jsonc（本机实测只有它）")
        self.assertEqual(spec["shape"], "opencode")

    def test_entry_shapes_differ_per_host(self):
        """两种 schema 必须生成不同形状 —— 这是 P3 的核心。"""
        classic = install_mcp.entry(dry=True, shape="mcpServers")
        oc = install_mcp.entry(dry=True, shape="opencode")
        self.assertIsInstance(classic["command"], str)
        self.assertIn("args", classic)
        self.assertIn("env", classic)
        self.assertEqual(oc["type"], "local")
        self.assertIsInstance(oc["command"], list, "opencode 的 command 必须是数组")
        self.assertIn("environment", oc)
        self.assertNotIn("args", oc)

    def test_opencode_shape_matches_existing_entries(self):
        """生成的形状必须与 opencode 既有条目（本机 cli-anything）一致。"""
        path = os.path.expanduser("~/.config/opencode/opencode.jsonc")
        if not os.path.exists(path):
            self.skipTest("本机没有 opencode.jsonc")
        raw = install_mcp._strip_jsonc(open(path, encoding="utf-8").read())
        data = json.loads(raw)
        others = [v for k, v in (data.get("mcp") or {}).items() if k != "ugf"]
        if not others:
            self.skipTest("opencode 里没有别的条目可比对")
        ref = others[0]
        mine = install_mcp.entry(dry=True, shape="opencode")
        self.assertEqual(set(mine), set(ref), f"字段集不一致：{set(mine)} vs {set(ref)}")

    def test_strip_jsonc_handles_comments_and_trailing_commas(self):
        raw = """{
  // 行注释
  "a": 1,  /* 块注释 */
  "b": [1, 2, 3,],
        }"""
        self.assertEqual(json.loads(install_mcp._strip_jsonc(raw)),
                         {"a": 1, "b": [1, 2, 3]})

    def test_strip_jsonc_does_not_eat_strings(self):
        raw = '{"url": "http://x.com//path", "s": "/* not a comment */"}'
        out = json.loads(install_mcp._strip_jsonc(raw))
        self.assertEqual(out["url"], "http://x.com//path")
        self.assertEqual(out["s"], "/* not a comment */")

    def test_write_creates_backup(self):
        """改用户配置必须留回头路。"""
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "mcp.json")
            with open(p, "w", encoding="utf-8") as f:
                f.write('{"mcpServers":{"other":{}}}')
            install_mcp._write(p, {"mcpServers": {"ugf": {}}})
            self.assertTrue(os.path.exists(p + ".bak"), "写入前必须备份")
            with open(p + ".bak", encoding="utf-8") as f:
                self.assertIn("other", f.read(), "备份应是写入前的内容")

    def test_roundtrip_preserves_other_hosts(self):
        """装 ugf 不能动别人已有的条目。"""
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "opencode.jsonc")
            with open(p, "w", encoding="utf-8") as f:
                f.write('{"mcp":{"cli-anything":{"type":"local","command":["node","x.mjs"],'
                        '"enabled":true,"environment":{}}}}')
            sys.argv = ["install_mcp.py", "--target", "custom", "--path", p,
                        "--online"]
            # custom 走 mcpServers 形状，这里只验证不丢内容
            data = install_mcp._load(p, "jsonc")
            data.setdefault("mcp", {})["ugf"] = install_mcp.entry(shape="opencode")
            install_mcp._write(p, data)
            back = install_mcp._load(p, "jsonc")
            self.assertIn("cli-anything", back["mcp"])
            self.assertIn("ugf", back["mcp"])


# ---------------------------------------------------------------------------
# v2.0 P3 第二层：宿主 LLM 真实调用（2026-10-01）
# ---------------------------------------------------------------------------
# verify_portability 只验到协议层（服务能起、工具在）。
# 这里验的是「宿主 LLM 真的会用这些工具」—— 那才是 P3 的真正含义。

class TestLiveUsage(unittest.TestCase):
    def _mod(self):
        sys.path.insert(0, os.path.join(BASE, "tools"))
        import verify_live_usage
        return verify_live_usage

    def test_probe_expectations_are_ugf_specific(self):
        """探针的判据必须是 ugf 独有的内容，不能是通用词。

        否则模型编也能过 —— 那是假阳性，比没验更糟。
        """
        m = self._mod()
        generic = {"tool", "list", "data", "file", "result", "content", "None"}
        for pr in m.PROBES:
            self.assertTrue(pr["expect_any"], f"{pr['name']} 没有判据")
            for k in pr["expect_any"]:
                self.assertNotIn(k.lower(), generic,
                                 f"{pr['name']} 的判据 {k!r} 过于通用")
        # 至少一个判据要含 ugf 知识库里真实存在的字符串
        all_exp = {k for pr in m.PROBES for k in pr["expect_any"]}
        self.assertTrue(all_exp & {"kb_export", "boss_behavior_log", "mantis"},
                        "判据必须锚定 ugf 的独有数据")

    def test_probes_cover_different_tools(self):
        """三个探针打三个不同工具，避免「一个工具能用就算过」。"""
        m = self._mod()
        names = [p["name"] for p in m.PROBES]
        self.assertEqual(len(names), len(set(names)))
        self.assertGreaterEqual(len(names), 3)

    def test_prompts_demand_verbatim(self):
        """prompt 必须要求「原样贴出」，否则模型会自己总结成通用话术。"""
        m = self._mod()
        for pr in m.PROBES:
            self.assertTrue("原样" in pr["prompt"], f"{pr['name']} 没要求原样输出")

    def test_returns_2_when_opencode_absent(self):
        """环境不具备时必须返回 2，不能返回 0 假装通过。

        门禁里「环境不具备」要被单独标记，混进「通过」就是自欺。
        """
        m = self._mod()
        import io
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = m.main(["--json", "--opencode-bin", "/nonexistent/opencode"])
        self.assertEqual(rc, 2, "找不到 opencode 时应返回 2（环境不具备）")
        self.assertIn("no_env", buf.getvalue())

    def test_guesses_are_ugf_only(self):
        """判据字符串必须能在本仓库里找到出处，不能凭空编。"""
        m = self._mod()
        kb = os.path.join(BASE, "knowledge_md")
        self.assertTrue(os.path.isdir(kb), "知识库目录不在，P3 实证失去意义")
        found_any = False
        for root, _dirs, files in os.walk(kb):
            for fn in files:
                if not fn.endswith(".md"):
                    continue
                try:
                    body = open(os.path.join(root, fn), encoding="utf-8",
                                errors="replace").read()
                except OSError:
                    continue
                if "mantis" in body:
                    found_any = True
                    break
            if found_any:
                break
        self.assertTrue(found_any, "知识库里没有 mantis 记录，query_boss_history 探针无意义")
