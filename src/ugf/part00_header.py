#!/usr/bin/env python3
"""
Universal-Game-Framework · 单文件游戏 Agent  (v3.2)
=====================================================

一个**能自己跑**的游戏智能体：不依赖任何别的 Agent，不需要装成 MCP 服务。
直接 `python agent.py run` 就能自己「看画面 → 预判 → 评估 → 出动作 → 查/写经验 → 复盘 → 汇报」。

设计原则（对应「轻量化 / 跑得快」）：
  1. **单文件**：所有能力都在这一个文件里，没有跨模块 import 链。
  2. **零 IPC**：旧版每回合要走 MCP 子进程 + JSON-RPC 往返 6 次；这里是进程内函数调用。
  3. **惰性依赖**：requests / yaml / pyautogui / cv2 全部按需 import，没装也能跑离线链路。
  4. **数学层零依赖**：预判与战斗评估是纯 math，不引入 numpy。

能力清单（原 16 个 MCP 工具的能力一个不少，只是从「工具调用」变成了「子命令/内部方法」）：
  知识库   kb list / kb search / kb write / kb append / kb export / kb import
  感知     perceive（run 内部每回合自动调用） / predict（手动看一帧+预判） / kb clean
  动作     action / set（= game_action / switch_set） / afk（= handle_afk）
  维护     kb boss（= query_boss_history） / kb tactic（= switch_tactic） / kb clean（= clean_cache）
  手册     guide（= ugf_guide）
  高层     run（主循环：视频学习、全实体预判、战斗评估、动态心态、组队协同、拟人操作、
          BOSS 记忆、死亡复盘、会话记忆/断点续玩、自动调参、自动汇报、游戏档案化）

⚠️ 声明：仅用于本地 AI 智能体技术研究。在 florr.io 官方服务器运行 bot 违反游戏服务条款，
   可能导致账号封禁。请在本地 / 自建 / 已授权环境使用。
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import difflib
import json
import math
import os
import random
import re
import shutil
import sys
import tarfile
import time
from collections import deque
from datetime import datetime
from typing import TypedDict


def _resolve_base_dir() -> str:
    """运行期数据目录解析（ROADMAP #21）。

    优先级：UGF_HOME 环境变量 > agent.py 所在目录（须为真实可写目录）> ~/.ugf。
    pip 安装或 zipapp 运行时代码目录不可写（甚至在压缩包内），
    知识库/日志/状态文件/配置档案一律落到用户目录，仓库内运行行为不变。
    """
    home = (os.getenv("UGF_HOME") or "").strip()
    if home:
        p = os.path.abspath(os.path.expanduser(home))
        with contextlib.suppress(OSError):
            os.makedirs(p, exist_ok=True)
        return p
    code_dir = os.path.dirname(os.path.abspath(__file__))
    if os.path.isdir(code_dir) and ".pyz" not in code_dir and os.access(code_dir, os.W_OK):
        return code_dir
    p = os.path.join(os.path.expanduser("~"), ".ugf")
    with contextlib.suppress(OSError):
        os.makedirs(p, exist_ok=True)
    return p


BASE_DIR = _resolve_base_dir()

VERSION = "3.2.0-single"


# ---------------------------------------------------------------------------
# 核心数据流类型（ROADMAP #4）：帧 / 预判实体 / 战斗评估 / 动作
# 全部 total=False：字段渐进演化时不破坏旧调用方
# ---------------------------------------------------------------------------
class FramePayload(TypedDict, total=False):
    """感知后端统一帧载荷。"""

    player: dict
    entities: list
    teammates: list
    afk_popup: bool
    error: str
    _fallback: str


class EntityPred(TypedDict, total=False):
    """预判输出的单实体。x/y_predict 为 None 表示置信不足，勿信预判坐标。"""

    raw_id: str
    rarity: str
    category: str
    role: str
    threat_score: float
    x_now: float
    y_now: float
    x_predict: float | None
    y_predict: float | None
    vx_per_sec: float
    vy_per_sec: float
    confidence: float
    prediction_trusted: bool
    model: str


class CombatEval(TypedDict, total=False):
    """战斗评估结果。decision ∈ fight / cautious_fight / retreat。"""

    decision: str
    recommended_set: str
    mindset: str
    enemy_threat: float
    threat_ratio: float
    has_highest_boss: bool
    retreat_reason: str


class ActionDict(TypedDict, total=False):
    """决策动作。source ∈ llm / kb / rule，move 时必带 x/y。"""

    action: str
    x: int
    y: int
    source: str


