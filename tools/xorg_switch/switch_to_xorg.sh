#!/bin/bash
# ============================================================
# 切到 Xorg 会话（解决 Wayland 下所有游戏都无法走 X11 抓屏的问题）
#
# 背景：当前是 GNOME on Wayland + Xwayland -rootless，
#      X 的 root 窗口不是真实屏幕 → import / xwd / mss 全部失效，
#      只能走 Mutter 合成器截图，固定 ~1573ms，无法优化。
#      换成 Xorg 后 X11 抓屏恢复，约 50ms，且**对所有游戏通用**
#      （原生客户端、网页游戏都适用）。
#
# 已验证的前置条件（缺任何一项都会导致开不了图形界面）：
#   ✓ /usr/share/xsessions/gnome-xorg.desktop 存在
#   ✓ xserver-xorg-core 2:21.1 已安装
#   ✓ xserver-xorg-input-all 已安装
#   ✓ gdm3 48.0 是当前显示管理器
#
# 回滚：见同目录 rollback_xorg.sh，或在 GDM 登录界面用齿轮选回
#      "GNOME on Wayland"（不需要命令行）。
# ============================================================
set -euo pipefail

CONF=/etc/gdm3/custom.conf

echo "═══ 1. 再次确认 Xorg 会话可用（这是能不能安全切换的前提）═══"
if [ ! -f /usr/share/xsessions/gnome-xorg.desktop ]; then
    echo "  ✗ 没有 gnome-xorg.desktop，切换会导致无法登录图形界面。"
    echo "    请先执行：sudo apt install xorg"
    exit 1
fi
echo "  ✓ /usr/share/xsessions/gnome-xorg.desktop"

for p in xserver-xorg-core xserver-xorg-input-all; do
    dpkg -s "$p" >/dev/null 2>&1 && echo "  ✓ $p 已安装" \
        || { echo "  ✗ 缺 $p，请先 sudo apt install $p"; exit 1; }
done

echo ""
echo "═══ 2. 备份配置 ═══"
BAK="$CONF.bak-$(date +%Y%m%d-%H%M%S)"
sudo cp -a "$CONF" "$BAK"
echo "  ✓ 已备份到 $BAK"
sudo -v   # 提前问一次密码，避免中途再要
echo "  ✓ 备份完成"

echo ""
echo "═══ 3. 写入 WaylandEnable=false ═══"
sudo python3 - "$CONF" <<'PYEOF'
import sys
path = sys.argv[1]
with open(path, encoding="utf-8") as f:
    lines = f.read().splitlines()

key = "WaylandEnable"
# 已有该键 → 就地替换
hit = 0
for i, ln in enumerate(lines):
    s = ln.strip()
    if s.startswith(key):
        lines[i] = "WaylandEnable=false"
        hit += 1
        break

if not hit:
    # 没有 → 插到 [Daemon] 段开头（必须在该段内，放段首最安全）
    idx = None
    for i, ln in enumerate(lines):
        if ln.strip() == "[Daemon]":
            idx = i + 1
            break
    if idx is None:
        # 极端情况：文件里没有 [Daemon] 段，直接追加一个
        lines += ["", "[Daemon]", "WaylandEnable=false"]
        print("  (未找到 [Daemon] 段，已在文件末尾追加)")
    else:
        lines.insert(idx, "WaylandEnable=false")
        print("  (未找到该键，已插入 [Daemon] 段)")

with open(path, "w", encoding="utf-8") as f:
    f.write("\n".join(lines) + "\n")
PYEOF

echo ""
echo "═══ 4. 验证写入结果 ═══"
sudo grep -n "^\[Daemon\]\|WaylandEnable" "$CONF" | head -5
if sudo grep -qE "^WaylandEnable=false" "$CONF"; then
    echo "  ✓ WaylandEnable=false 已生效"
else
    echo "  ✗ 写入失败，请检查 $CONF"; exit 1
fi

echo ""
echo "════════════════════════════════════════════════"
echo " 配置已改好，**现在需要重启生效**。"
echo ""
echo "   sudo reboot"
echo ""
echo " 重启后在 GDM 登录界面："
echo "   · 正常输密码登录即可（默认就是 Xorg 了）"
echo "   · 如果黑屏/异常，点右下角齿轮手动选 GNOME on Xorg"
echo ""
echo " 登录后先自检（应该是 x11）："
echo "   echo \$XDG_SESSION_TYPE      # 期望 x11"
echo "   bash /tmp/opencode/verify_xorg_capture.sh"
echo ""
echo " 回滚（两种方式）："
echo "   1) 登录界面齿轮选 'GNOME on Wayland' —— 不用命令行，最快"
echo "   2) sudo bash /tmp/opencode/rollback_xorg.sh"
echo "════════════════════════════════════════════════"
