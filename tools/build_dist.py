#!/usr/bin/env python3
"""
FlorrVLM-Agent 系统安装包打包  tools/build_dist.py  (v2.0)
"""
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "dist")
PKG = "florrvlm-agent"
VERSION = os.getenv("PKG_VERSION", "2.0.0")

SHIP_DIRS = ["game_profiles", "skills", "tools"]
SHIP_FILES = [
    "agent_cli.py", "agent_main.py", "admin_panel.py", "auto_tuner.py",
    "boot_check.py", "cli_ui.py", "combat_judge.py", "config.py", "config.yaml",
    "game_profile_check.py", "kb_maintainer.py", "mcp_connector.py",
    "mcp_connectors.yaml", "mcp_server.py", "perception_server.py", "player_trick.py",
    "predictor.py", "report_notifier.py", "resource_guard.py", "session.py",
    "skill_manager.py", "video_learner.py", "video_sources.py", "vlm_detect.py",
    "requirements.txt", "ROADMAP.md", "README.md", ".env.example",
]


def ensure_out():
    os.makedirs(OUT, exist_ok=True)


def stage_tree(dest: str):
    os.makedirs(dest, exist_ok=True)
    for d in SHIP_DIRS:
        src = os.path.join(ROOT, d)
        if os.path.isdir(src):
            shutil.copytree(src, os.path.join(dest, d), dirs_exist_ok=True)
    for f in SHIP_FILES:
        s = os.path.join(ROOT, f)
        if os.path.isfile(s):
            shutil.copy2(s, os.path.join(dest, f))
    for skip in ("__pycache__", "run_logs", "video_frames", "knowledge_md",
                 "session_history.json", "agent_state.json", "tuned_overrides.yaml"):
        p = os.path.join(dest, skip)
        if os.path.isdir(p):
            shutil.rmtree(p, ignore_errors=True)
        elif os.path.isfile(p):
            os.remove(p)
    # 递归清掉嵌套 __pycache__
    for rootdir, dirs, _ in os.walk(dest, topdown=False):
        for d in dirs:
            if d == "__pycache__":
                shutil.rmtree(os.path.join(rootdir, d), ignore_errors=True)


def build_deb() -> str:
    if not shutil.which("dpkg-deb"):
        raise RuntimeError("缺少 dpkg-deb")
    tmp = tempfile.mkdtemp(prefix="florrvlm_deb_")
    INSTALL_LIB = f"/usr/lib/{PKG}"
    try:
        rootpkg = os.path.join(tmp, "pkg")
        lib = os.path.join(rootpkg, "usr", "lib", PKG)
        bin = os.path.join(rootpkg, "usr", "bin")
        doc = os.path.join(rootpkg, "usr", "share", "doc", PKG)
        os.makedirs(bin, exist_ok=True)
        os.makedirs(doc, exist_ok=True)
        stage_tree(lib)

        # Agent 命令
        launcher = os.path.join(bin, "florrvlm-agent")
        with open(launcher, "w", encoding="utf-8") as f:
            f.write(
                "#!/usr/bin/env python3\n"
                "import os, sys\n"
                f"sys.path.insert(0, '{INSTALL_LIB}')\n"
                f"os.environ['AGENT_ROOT'] = '{INSTALL_LIB}'\n"
                "from agent_cli import main\n"
                "if __name__ == '__main__':\n"
                "    main()\n"
            )
        os.chmod(launcher, 0o755)

        # WebUI 命令
        ui_launcher = os.path.join(bin, "florrvlm-ui")
        with open(ui_launcher, "w", encoding="utf-8") as f:
            f.write(
                "#!/usr/bin/env python3\n"
                "import os, sys, runpy\n"
                f"sys.path.insert(0, '{INSTALL_LIB}')\n"
                f"os.environ['AGENT_ROOT'] = '{INSTALL_LIB}'\n"
                f"runpy.run_path('{INSTALL_LIB}/admin_panel.py', run_name='__main__')\n"
            )
        os.chmod(ui_launcher, 0o755)

        with open(os.path.join(doc, "README.txt"), "w", encoding="utf-8") as f:
            f.write("FlorrVLM-Agent v%s\n" % VERSION)

        debin = os.path.join(rootpkg, "DEBIAN")
        os.makedirs(debin, exist_ok=True)
        with open(os.path.join(debin, "control"), "w", encoding="utf-8") as f:
            f.write(
                "Package: %s\nVersion: %s\nSection: utils\nPriority: optional\n"
                "Architecture: amd64\n"
                "Depends: python3, python3-yaml, python3-requests, python3-dotenv\n"
                "Maintainer: FlorrVLM-Agent\n"
                "Description: Visual game agent with VLM perception and MCP tools\n"
                % (PKG, VERSION)
            )
        with open(os.path.join(debin, "conffiles"), "w", encoding="utf-8") as f:
            f.write("/usr/lib/%s/config.yaml\n" % PKG)
        with open(os.path.join(debin, "postinst"), "w", encoding="utf-8") as f:
            f.write("#!/bin/sh\nset -e\nmkdir -p /var/lib/%s\n" % PKG)
        os.chmod(os.path.join(debin, "postinst"), 0o755)

        sums = []
        for rootdir, _, files in os.walk(rootpkg):
            if rootdir.endswith("DEBIAN"):
                continue
            for fn in files:
                fp = os.path.join(rootdir, fn)
                rel = os.path.join(os.path.relpath(rootdir, rootpkg), fn)
                h = subprocess.check_output(["md5sum", fp], text=True).split()[0]
                sums.append(f"{h}  {rel}")
        with open(os.path.join(debin, "md5sums"), "w", encoding="utf-8") as f:
            f.write("\n".join(sums) + "\n")

        out = os.path.join(OUT, f"{PKG}_{VERSION}_amd64.deb")
        subprocess.check_call(["dpkg-deb", "--build", rootpkg, out])
        return out
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def build_portable() -> str:
    tmp = tempfile.mkdtemp(prefix="florrvlm_port_")
    try:
        rootdir = os.path.join(tmp, PKG)
        stage_tree(rootdir)
        with open(os.path.join(rootdir, "florrvlm-agent"), "w", encoding="utf-8") as f:
            f.write(
                "#!/usr/bin/env python3\nimport os, sys\n"
                "sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))\n"
                "from agent_cli import main\nmain()\n"
            )
        os.chmod(os.path.join(rootdir, "florrvlm-agent"), 0o755)
        out = os.path.join(OUT, f"{PKG}_{VERSION}_portable.tar.gz")
        subprocess.check_call(["tar", "-C", tmp, "-czf", out, PKG])
        return out
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main(kinds: list = None):
    want = set(kinds) or {"all"}
    ensure_out()
    results = []
    if want & {"deb", "all"}:
        results.append(("Linux .deb", build_deb()))
    if want & {"portable", "all"}:
        results.append(("Linux 便携版", build_portable()))
    for label, path in results:
        size = os.path.getsize(path) / 1024
        print(f"  {label:12s} {path}  ({size:.0f} KB)")


if __name__ == "__main__":
    main(sys.argv[1:] or ["all"])
