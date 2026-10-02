#!/bin/bash
# 回滚到 Wayland。
# 注意：WaylandEnable=false 是**移除限制**（让所有游戏都能走 X11 抓屏），
# 通常不需要回滚。真正需要回滚的情况是「切到 Xorg 后开不了图形界面」。
set -euo pipefail

CONF=""
for c in /etc/gdm3/daemon.conf /etc/gdm3/custom.conf; do
    [ -f "$c" ] && { CONF="$c"; break; }
done
[ -n "$CONF" ] || { echo "  ✗ 找不到 gdm 配置文件"; exit 1; }

echo "═══ 目标配置: $CONF ═══"
echo ""
echo "  当前设置："
sudo grep -nE "^\s*(WaylandEnable|AutomaticLoginEnable)" "$CONF" || echo "    (无显式设置)"

echo ""
echo "  备份文件："
BAK=$(ls -1t "$CONF".bak-* 2>/dev/null | head -3 || true)
[ -n "$BAK" ] && echo "$BAK" | sed 's/^/    /' || echo "    无"

echo ""
read -rp "  输入 yes 确认回滚到 Wayland: " ans
[ "$ans" = "yes" ] || { echo "  已取消"; exit 0; }

sudo python3 - "$CONF" <<'PY'
import re, sys
p = sys.argv[1]
lines = open(p, encoding="utf-8").read().splitlines()
for i, ln in enumerate(lines):
    if re.match(r'^\s*WaylandEnable\s*=', ln, re.I):
        lines[i] = "#WaylandEnable=false"     # 注释掉而不是删，保留线索
        break
open(p, "w", encoding="utf-8").write("\n".join(lines) + "\n")
print("  ✓ WaylandEnable 已注释掉")
PY

echo ""
echo "  重启生效：sudo reboot"
echo "  若这台机器同时关了自动登录，重启后可在登录界面齿轮选回 Wayland。"
