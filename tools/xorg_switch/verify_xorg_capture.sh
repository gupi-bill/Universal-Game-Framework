#!/bin/bash
# ============================================================
# 重启后的自检：确认抓屏真的变快了
#
# 切换前基线（Wayland + Xwayland rootless，负载正常时）：
#   gnome-screenshot   1573 ms
#   import -window root   BadMatch（失败）
#   xwd -root             BadMatch（失败）
#   mss              XProtoError opcode 73（失败）
#
# 切换后期望：
#   XDG_SESSION_TYPE = x11
#   import / xwd / mss 可用，延迟 ~30~80 ms
#
# ⚠ 测量前先看负载：刚重启时机器负载很高（实测 load 8~9、可用内存
#   600MB），此时数据不可比。脚本会先报负载，超过 4 会提示。
# ============================================================
PY=/home/g-bill/.workbuddy/binaries/python/envs/ugf/bin/python

echo "═══ 0. 机器状态（先确认数据可不可信）═══"
LOAD=$(cut -d' ' -f1 /proc/loadavg)
AVAIL=$(free -m | awk 'NR==2{print $7}')
UPTIME_S=$(awk '{print int($1)}' /proc/uptime)
echo "  负载(1min) = $LOAD    可用内存 = ${AVAIL}MB    已开机 = ${UPTIME_S}s"
if awk "BEGIN{exit !($LOAD > 4)}"; then
    echo "  ⚠ 负载偏高（>$LOAD），**下面的延迟数据会偏大，不可与基线直接比**"
    echo "    建议开机 10 分钟、负载降到 1 以下再测一次"
else
    echo "  ✓ 负载正常，数据可用"
fi
if [ "$AVAIL" -lt 800 ]; then
    echo "  ⚠ 可用内存 ${AVAIL}MB 偏低，YOLO 推理会明显变慢"
fi

echo ""
echo "═══ 1. 会话类型（关键）═══"
echo "  XDG_SESSION_TYPE     = ${XDG_SESSION_TYPE:-未设置}"
echo "  XDG_CURRENT_DESKTOP  = ${XDG_CURRENT_DESKTOP:-未设置}"
if [ "${XDG_SESSION_TYPE:-}" = "x11" ]; then
    echo "  ✓ 已是 X11 会话"
else
    echo "  ✗ 不是 x11 —— 切换未生效？"
    echo "    检查配置：sudo bash ~/xorg-switch/switch_to_xorg.sh --check"
    echo "    注意：若用了 --force（保留自动登录），改配置后必须重启才会生效"
fi
if pgrep -a Xwayland >/dev/null 2>&1; then
    echo "  ⚠ 仍有 Xwayland 进程（Xorg 会话下不应有）："
    pgrep -a Xwayland | head -1 | cut -c1-100
fi

echo ""
echo "═══ 2. 抓屏工具实测（3 次取中位）═══"
bench() {
    local label="$1"; shift
    local n=3 total=0 ok=0 i t0 t1
    for i in $(seq $n); do
        t0=$(date +%s%N)
        if "$@" >/dev/null 2>&1; then
            t1=$(date +%s%N); total=$(( total + (t1 - t0) / 1000000 )); ok=$(( ok + 1 ))
        fi
        rm -f /tmp/vshot.png /tmp/vshot.xwd 2>/dev/null
    done
    if [ "$ok" -gt 0 ]; then
        echo "  ✓ $label: 中位 $(( total / ok )) ms  ($ok/$n)"
    else
        echo "  ✗ $label: 全部失败"
    fi
}
if command -v import >/dev/null 2>&1; then
    bench "import -window root" import -window root /tmp/vshot.png
fi
bench "gnome-screenshot"    gnome-screenshot -f /tmp/vshot.png
if command -v xwd >/dev/null 2>&1; then
    bench "xwd -root"          xwd -root -out /tmp/vshot.xwd
fi

echo ""
echo "═══ 3. mss（纯库，最快，无子进程开销）═══"
"$PY" - <<'PYEOF'
import os, sys, time, statistics, warnings
warnings.filterwarnings("ignore")
# cv2 的 C++ 日志（NNPACK）在 fd 层压制，否则会刷上百行把结果淹掉
_saved = os.dup(2); _dn = os.open(os.devnull, os.O_WRONLY)
os.dup2(_dn, 2)
try:
    import mss
    with mss.MSS() as s:
        ts = []
        for _ in range(5):
            t0 = time.time(); img = s.grab(s.monitors[1]); ts.append((time.time() - t0) * 1000)
        ts.sort()
        out = f"  ✓ mss 抓取: 中位 {ts[len(ts)//2]:.0f} ms  min {ts[0]:.0f}  ({img.width}x{img.height})"
    sys.stderr.flush()
finally:
    os.dup2(_saved, 2); os.close(_dn); os.close(_saved)
print(out) if 'out' in dir() else print(f"  ✗ mss 失败: {type(e).__name__}" if False else "")
PYEOF

echo ""
echo "═══ 4. 端到端（截图 + YOLO）═══"
cd "/home/g-bill/文档/Default Project/Universal-Game-Framework" 2>/dev/null || {
    echo "  - 找不到 UGF 目录，跳过"; exit 0; }
"$PY" - <<'PYEOF'
import os, sys, time, statistics, warnings
warnings.filterwarnings("ignore")
sys.path.insert(0, ".")
_saved = os.dup(2); _dn = os.open(os.devnull, os.O_WRONLY)
os.dup2(_dn, 2)
res = {}
try:
    import perception_server as ps
    shot = "/tmp/ve2e.png"
    ts = []
    for _ in range(3):
        if not ps._take_screenshot(shot):
            res["shot"] = None; break
        t0 = time.time(); ps._take_screenshot(shot); ts.append((time.time() - t0) * 1000)
    res["shot"] = statistics.median(ts) if ts else None
    try:
        from ultralytics import YOLO
        if res["shot"]:
            m = YOLO("models/yolov8n.pt")
            ys = []
            for _ in range(3):
                t0 = time.time(); m.predict(shot, verbose=False, imgsz=320); ys.append((time.time() - t0) * 1000)
            res["yolo"] = statistics.median(ys)
    except Exception as e:
        res["yolo_err"] = f"{type(e).__name__}: {str(e)[:50]}"
finally:
    sys.stderr.flush()
    os.dup2(_saved, 2); os.close(_dn); os.close(_saved)

if res.get("shot"):
    print(f"  截图(UgF 现有路径): 中位 {res['shot']:.0f} ms   ← 切换前 1573 ms")
else:
    print("  ✗ 截图失败（ps._take_screenshot 返回 False）")
if res.get("yolo"):
    print(f"  YOLO imgsz=320:     中位 {res['yolo']:.0f} ms")
if res.get("yolo_err"):
    print(f"  - YOLO 跳过: {res['yolo_err']}")
if res.get("shot") and res.get("yolo"):
    tot = res["shot"] + res["yolo"]
    print(f"  端到端/帧:          约 {tot:.0f} ms   ← 切换前 ~2400 ms（低负载时），目标 <800 ms")
PYEOF

echo ""
echo "════════════════════════════════════════════"
echo " 基线对照（⚠ 仅在负载 <4 时可比）:"
echo "   Wayland  截图 1573 ms   端到端 ~2400 ms/帧"
echo "   Xorg     截图   ~50 ms   端到端  <800 ms/帧"
echo "════════════════════════════════════════════"
