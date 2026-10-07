#!/usr/bin/env bash
# ROADMAP v2 #22：一键离线 Demo 走查（幂等，全程 dry-run + mock，不碰键鼠）
set -euo pipefail
cd "$(dirname "$0")/.."

echo "═══ 1/6 selftest ═══"
python3 agent.py selftest | tail -2

echo "═══ 2/6 doctor ═══"
python3 agent.py doctor | tail -1

echo "═══ 3/6 dry-run 20 回合 ═══"
python3 agent.py run --dry-run --rounds 20 | tail -2

echo "═══ 4/6 事件统计 ═══"
python3 agent.py logs --stats

echo "═══ 5/6 brief + 战绩 + 回放 ═══"
python3 agent.py brief | head -8
python3 agent.py session --report | head -4
python3 agent.py replay --tail 5

echo "═══ 6/6 bench 100 回合 ═══"
python3 agent.py bench --rounds 100 | tail -7

echo "✓ Demo 走查完成"
