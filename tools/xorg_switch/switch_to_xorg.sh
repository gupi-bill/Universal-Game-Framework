#!/bin/bash
# ============================================================
# 切到 Xorg 会话 —— 修正版
#
# v1 的三个错误（都已在真实机器上暴露）：
#   1. 配置文件名错了：GDM 48（本机）用 /etc/gdm3/daemon.conf，
#      不是 /etc/gdm3/custom.conf。后者根本不存在，脚本第一步就失败。
#   2. 没查自动登录：本机 daemon.conf 里 AutomaticLoginEnable=True，
#      自动登录**不给会话选择器**，所以"登录界面用齿轮切回 Wayland"
#      这条退路是不存在的。
#   3. 因此默认改为「临时关自动登录」，让你能在登录界面手动选 Xorg，
#      保留退路；验证通过后用 --restore-autologin 恢复。
#
# 已验证的前置条件（缺任一项会导致开不了图形界面）：
#   ✓ /usr/share/xsessions/gnome-xorg.desktop 存在
#   ✓ xserver-xorg-core / xserver-xorg-input-all 已安装
#   ✓ gdm3 48.0-2 是当前显示管理器
# ============================================================
set -euo pipefail

MODE="${1:-}"

# ---- 定位配置文件：daemon.conf(GDM≥46) 优先，回落 custom.conf ----
CONF=""
for c in /etc/gdm3/daemon.conf /etc/gdm3/custom.conf; do
    [ -f "$c" ] && { CONF="$c"; break; }
done
if [ -z "$CONF" ]; then
    echo "  ✗ /etc/gdm3/daemon.conf 和 custom.conf 都不存在"
    echo "    停止，不做任何修改。"
    exit 1
fi

echo "═══ 目标配置文件: $CONF ═══"
echo ""
echo "═══ 0. 前置条件复查（缺任一项就中止）═══"
if [ ! -f /usr/share/xsessions/gnome-xorg.desktop ]; then
    echo "  ✗ 缺 gnome-xorg.desktop → 切换后无法登录图形界面"
    echo "    先执行：sudo apt install xorg"
    exit 1
fi
echo "  ✓ gnome-xorg.desocket 存在" 2>/dev/null || echo "  ✓ gnome-xorg.desktop 存在"
for p in xserver-xorg-core xserver-xorg-input-all; do
    if dpkg -s "$p" >/dev/null 2>&1; then echo "  ✓ $p"
    else echo "  ✗ 缺 $p → 先 sudo apt install $p"; exit 1; fi
done
echo "  ✓ gdm3 $(dpkg-query -W -f='${Version}' gdm3 2>/dev/null)"

sudo -v
echo ""

case "$MODE" in
--restore-autologin)
    echo "═══ 恢复自动登录 ═══"
    sudo python3 - "$CONF" <<'PY'
import re, sys
p = sys.argv[1]
s = open(p, encoding="utf-8").read()
s = re.sub(r'(?mi)^(\s*)#?\s*AutomaticLoginEnable\s*=.*$', r'\1AutomaticLoginEnable=True', s)
open(p, "w", encoding="utf-8").write(s)
print("  ✓ AutomaticLoginEnable=True")
PY
    sudo grep -nE "AutomaticLogin|WaylandEnable" "$CONF" | grep -v '^\s*#'
    echo "  重启生效：sudo reboot"
    exit 0
    ;;
--rollback)
    echo "═══ 回滚到 Wayland ═══"
    BAK=$(ls -1t "$CONF".bak-* 2>/dev/null | head -1 || true)
    if [ -z "$BAK" ]; then
        echo "  未找到备份，改为直接注释掉该键："
        sudo sed -i 's/^WaylandEnable=/#WaylandEnable=/' "$CONF"
    else
        echo "  恢复备份：$BAK"
        sudo cp -a "$BAK" "$CONF"
    fi
    sudo grep -n "WaylandEnable" "$CONF" | head -2
    echo "  重启生效：sudo reboot"
    exit 0
    ;;
--check)
    echo "═══ 当前状态 ═══"
    sudo grep -nE "^\s*WaylandEnable|^\s*AutomaticLoginEnable" "$CONF"
    echo ""
    echo "  当前会话：XDG_SESSION_TYPE=${XDG_SESSION_TYPE:-?}  (期望 x11)"
    exit 0
    ;;
"")
    echo "用法："
    echo "  sudo bash $0 --safe              # 推荐：临时关自动登录 + 切 Xorg"
    echo "  sudo bash $0 --force             # 只切 Xorg，保留自动登录（无退路）"
    echo "  sudo bash $0 --restore-autologin # 验证通过后恢复自动登录"
    echo "  sudo bash $0 --rollback          # 回滚到 Wayland"
    echo "  sudo bash $0 --check             # 只看当前状态"
    exit 0
    ;;
esac

# ---------------- 实际切换 ----------------
echo "═══ 1. 备份 ═══"
BAK="$CONF.bak-$(date +%Y%m%d-%H%M%S)"
sudo cp -a "$CONF" "$BAK"
echo "  ✓ $BAK"

echo ""
echo "═══ 2. 改配置 ═══"
if [ "$MODE" = "--safe" ]; then
    echo "  · WaylandEnable=false（切 Xorg）"
    echo "  · AutomaticLoginEnable=False（临时关自动登录，保留会话选择器作为退路）"
    sudo python3 - "$CONF" safe <<'PY'
import re, sys
p, mode = sys.argv[1], sys.argv[2]
lines = open(p, encoding="utf-8").read().splitlines()

def set_key(lines, key, value, allow_commented=True):
    """把 key=value 写进 [daemon] 段；已存在的（含注释掉的）就地替换。"""
    for i, ln in enumerate(lines):
        s = ln.strip()
        # 匹配：WaylandEnable=... 或 #WaylandEnable=... 或 # WaylandEnable=...
        if re.match(rf'^(#\s*)?{key}\s*=', s, re.I):
            lines[i] = f"{key}={value}"
            return lines, "replaced"
    # 没有 → 插到 [daemon] 段首
    for i, ln in enumerate(lines):
        if ln.strip().lower() == "[daemon]":
            lines.insert(i + 1, f"{key}={value}")
            return lines, "inserted"
    lines += ["", "[daemon]", f"{key}={value}"]
    return lines, "appended"

lines, how = set_key(lines, "WaylandEnable", "false")
print(f"  WaylandEnable → ({how})")
if mode == "safe":
    lines, how = set_key(lines, "AutomaticLoginEnable", "False")
    print(f"  AutomaticLoginEnable → False ({how})")
open(p, "w", encoding="utf-8").write("\n".join(lines) + "\n")
PY
else
    echo "  · WaylandEnable=false（切 Xorg，保留自动登录）"
    sudo python3 - "$CONF" <<'PY'
import re, sys
p = sys.argv[1]
lines = open(p, encoding="utf-8").read().splitlines()
for i, ln in enumerate(lines):
    if re.match(r'^(#\s*)?WaylandEnable\s*=', ln.strip(), re.I):
        lines[i] = "WaylandEnable=false"; break
else:
    for i, ln in enumerate(lines):
        if ln.strip().lower() == "[daemon]":
            lines.insert(i + 1, "WaylandEnable=false"); break
open(p, "w", encoding="utf-8").write("\n".join(lines) + "\n")
PY
fi

echo ""
echo "═══ 3. 验证 ═══"
sudo grep -nE "^\s*(WaylandEnable|AutomaticLoginEnable)" "$CONF"
if ! sudo grep -qE "^WaylandEnable=false" "$CONF"; then
    echo "  ✗ 写入失败，未做其他改动"; exit 1
fi
echo "  ✓ WaylandEnable=false 已生效"

echo ""
echo "════════════════════════════════════════════════"
if [ "$MODE" = "--safe" ]; then
cat <<'EOT'
 现在重启：  sudo reboot

 登录界面会要求输密码（自动登录已临时关闭）——
 这是**故意**的，为了让你能选会话。

   · 正常情况：输密码直接进（默认已是 Xorg）
   · 想手动选：点右下角齿轮 → 选 "GNOME on Xorg"
   · 想退出：  齿轮选 "GNOME on Wayland" 就能当场回退，不用重启

 进系统后先自检：
   echo $XDG_SESSION_TYPE                # 期望 x11
   bash ~/xorg-switch/verify_xorg_capture.sh

 确认没问题后恢复自动登录：
   sudo bash ~/xorg-switch/switch_to_xorg.sh --restore-autologin
   sudo reboot
EOT
else
cat <<'EOT'
 现在重启：  sudo reboot

 ⚠ 你选了 --force：自动登录仍然开启，所以**登录界面不会出现会话选择器**，
   万一 Xorg 起不来，只能用 Ctrl+Alt+F2 切 tty 后：
       sudo cp -a /etc/gdm3/daemon.conf.bak-* /etc/gdm3/daemon.conf
       sudo reboot

 进系统后自检：
   echo $XDG_SESSION_TYPE                # 期望 x11
   bash ~/xorg-switch/verify_xorg_capture.sh
EOT
fi
echo "════════════════════════════════════════════════"
