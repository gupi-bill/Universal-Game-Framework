#!/bin/bash
# 回滚到 Wayland 会话（把 custom.conf 恢复成切换前的状态）
set -euo pipefail
CONF=/etc/gdm3/custom.conf

echo "═══ 找最近的备份 ═══"
BAK=$(ls -1t "$CONF".bak-* 2>/dev/null | head -1 || true)
if [ -z "$BAK" ]; then
    echo "  ✗ 找不到备份文件（$CONF.bak-*）"
    echo "    改为直接删除该键："
    echo "    sudo sed -i '/^WaylandEnable=/d' $CONF && sudo reboot"
    exit 1
fi
echo "  找到: $BAK"

echo ""
echo "═══ 确认要恢复吗 ═══"
echo "  备份内容里的 WaylandEnable 设置："
grep -n "WaylandEnable" "$BAK" || echo "    （备份里没有该键 = 恢复后回到默认 Wayland）"
echo ""
read -rp "  输入 yes 确认恢复: " ans
[ "$ans" = "yes" ] || { echo "  已取消"; exit 0; }

sudo cp -a "$BAK" "$CONF"
echo "  ✓ 已恢复 $CONF"
echo ""
echo "  重启生效：sudo reboot"
