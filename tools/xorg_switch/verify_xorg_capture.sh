#!/bin/bash
# ============================================================
# 重启到 Xorg 后的自检：确认抓屏真的变快了
#
# 切换前的基线（Wayland + rootless Xwayland）：
#   gnome-screenshot   1573 ms
#   import -window root   BadMatch（失败）
#   xwd -root             BadMatch（失败）
#   mss              XProtoError opcode 73（失败）
#
# 切换后期望：
#   XDG_SESSION_TYPE = x11
#   import / xwd / mss 可用，延迟 ~30~80 ms
# ============================================================
PY=/home/g-bill/.workbuddy/binaries/python/envs/ugf/bin/python
CHROME=/home/g-bill/chrome/opt/google/chrome/chrome

echo "═══ 1. 会话类型（关键）═══"
echo "  XDG_SESSION_TYPE = ${XDG_SESSION_TYPE:-未设置}"
echo "  XDG_CURRENT_DESKTOP = ${XDG_CURRENT_DESKTOP:-未设置}"
if [ "${XDG_SESSION_TYPE:-}" = "x11" ]; then
    echo "  ✓ 已是 X11 会话"
else
    echo "  ⚠ 不是 x11（可能是 wayland）—— 切换未生效？"
    echo "    检查：grep WaylandEnable /etc/gdm3/custom.conf"
fi
echo ""
echo "  Xwayland 进程（Xorg 会话下不应有）："
pgrep -a Xwayland 2>/dev/null | head -2 || echo "    无 ✓"

echo ""
echo "═══ 2. 抓屏工具实测（各跑 3 次取中位）═══"
bench() {   # bench <标签> <命令...>
    local label="$1"; shift
    local n=3 total=0 ok=0
    for i in $(seq $n); do
        local t0 t1
        t0=$(date +%s%N)
        if "$@" >/dev/null 2>&1; then
            t1=$(date +%s%N)
            total=$(( total + (t1 - t0) / 1000000 ))
            ok=$(( ok + 1 ))
        fi
        rm -f /tmp/verify_shot.png /tmp/verify_shot.xwd 2>/dev/null
    done
    if [ "$ok" -gt 0 ]; then
        echo "  ✓ $label: 中位 $(( total / ok )) ms  ($ok/$n 成功)"
    else
        echo "  ✗ $label: 全部失败"
    fi
}

bench "import -window root"  import -window root /tmp/verify_shot.png
bench "gnome-screenshot"     gnome-screenshot -f /tmp/verify_shot.png

if command -v xwd >/dev/null 2>&1; then
    bench "xwd -root" xwd -root -out /tmp/verify_shot.xwd
fi

echo ""
echo "═══ 3. mss（纯库，最快）═══"
"$PY" - <<'PYEOF'
import time, statistics, warnings
warnings.filterwarnings("ignore")
try:
    import mss
except ImportError:
    print("  - mss 未安装（pip install mss）"); raise SystemExit
try:
    with mss.MSS() as s:
        ts = []
        for _ in range(5):
            t0 = time.time(); img = s.grab(s.monitors[1]); ts.append((time.time()-t0)*1000)
        ts.sort()
        print(f"  ✓ mss 抓取: 中位 {ts[len(ts)//2]:.0f} ms  min {ts[0]:.0f}  ({img.width}x{img.height})")
except Exception as e:
    print(f"  ✗ mss 失败: {type(e).__name__}: {str(e)[:70]}")
PYEOF

echo ""
echo "═══ 4. 端到端（截图 + YOLO）═══"
cd "/home/g-bill/文档/Default Project/Universal-Game-Framework" 2>/dev/null && \
"$PY" - <<'PYEOF'
import os, sys, time, statistics
sys.path.insert(0, ".")
try:
    import perception_server as ps
    from ultralytics import YOLO
except Exception as e:
    print(f"  - 依赖不全，跳过: {type(e).__name__}"); raise SystemExit
shot = "/tmp/verify_e2e.png"
ts = []
for _ in range(3):
    if ps._take_screenshot(shot):
        t0 = time.time(); ps._take_screenshot(shot); ts.append((time.time()-t0)*1000)
if ts:
    print(f"  截图(UgF 现有路径): 中位 {statistics.median(ts):.0f} ms   ← 切换前 1573 ms")
    try:
        m = YOLO("models/yolov8n.pt")
        ys = []
        for _ in range(3):
            t0 = time.time(); m.predict(shot, verbose=False, imgsz=320); ys.append((time.time()-t0)*1000)
        print(f"  YOLO imgsz=320:     中位 {statistics.median(ys):.0f} ms")
        print(f"  端到端/帧:          约 {statistics.median(ts)+statistics.median(ys):.0f} ms"
              f"   ← 切换前 ~2400 ms，目标 500 ms")
    except Exception as e:
        print(f"  - YOLO 跳过: {type(e).__name__}")
PYEOF

echo ""
echo "════════════════════════════════════════════════"
echo " 对比基线："
echo "   Wayland(切换前)  截图 1573 ms   端到端 ~2400 ms/帧"
echo "   目标             截图 ~50 ms     端到端 < 800 ms/帧"
echo "════════════════════════════════════════════════"
