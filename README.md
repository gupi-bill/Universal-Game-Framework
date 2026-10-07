<div align="center">

# 🎮 Universal-Game-Framework

**一个能自己跑的单文件游戏 Agent**

看画面 → 预判 → 评估 → 出动作 → 查/写经验 → 复盘 → 汇报 → 学习，全在一个 `agent.py` 里。

> ⚠️ **声明**：仅用于本地 AI 智能体技术研究。在 florr.io 官方服务器运行 bot 违反游戏服务条款，可能导致账号封禁。请在本地 / 自建 / 已授权环境使用。

</div>

---

## 这是什么（给零基础看的）

想象一个"游戏代打机器人"，它每一步都做这几件事：

1. **看**：拿到当前画面里有什么（自己血量、怪物、队友）。
2. **算**：怪物下一秒会走到哪、打得过还是打不过。
3. **查**：翻自己的"经验本"（知识库），看以前遇到这种情况该怎么办。
4. **动**：按经验做出移动 / 攻击 / 切套装的动作。
5. **记**：打完一局写复盘，把有用的经验继续写进本子。

这个"机器人"就是 [agent.py](file:///workspace/Universal-Game-Framework/agent.py) ——
**一个文件、一条命令就能开跑**，不需要装 MCP 服务、不需要别的 Agent 托管。

---

## 快速开始

```bash
# 1. 先离线试跑（不碰键鼠、不需要密钥，最安全）
python agent.py selftest            # 全链路自检，应输出 19/19 通过
python agent.py run --dry-run --rounds 20

# 2. 看运行模式和手册
python agent.py mode
python agent.py guide

# 3. 真机开跑（需要外部感知服务 + 装了 pyautogui）
pip install "pyautogui>=0.9.54"     # 有显示器 / X server 才需要
python agent.py run
```

依赖极简：离线链路**零第三方库**即可跑通；真实运行建议 `pip install -r requirements.txt`（只有 `pyyaml` + `requests`）。

---

## 全部命令

| 命令 | 作用 |
| --- | --- |
| `run` | 主循环（`--rounds N` 限制回合 / `--interval S` 间隔 / `--dry-run` 空跑） |
| `mode` | 查看运行模式（dry-run？感知后端？LLM 开没开？当前游戏？） |
| `selftest` | 离线自检：跑通全链路并断言关键产物 |
| `perceive` | 手工取一帧画面状态 |
| `predict` | 手工取一帧 + 全实体 1.2s 预判 |
| `action` | 执行动作：`action move --x 100 --y 200` / `action attack` |
| `set` | 切换套装：`set retreat` |
| `afk` | AFK 弹窗处理指引 |
| `kb` | 知识库：`list` / `search` / `write` / `append` / `export` / `import` / `boss` / `tactic` / `clean` / `maintain` |
| `learn` | 视频学习：`learn video.mp4` 或 `learn --url <链接> --frames 5` |
| `report` | 生成对局报告（写 `run_logs/`，可配 Webhook 推送） |
| `session` | 看会话记忆与历史战绩 |
| `tune` | 自动调参：`tune --status` / `tune --reset` |
| `guide` | 打印使用手册 |

---

## 能力清单（一个不少）

| 能力 | 在哪体现 |
| --- | --- |
| 感知 | `Perception`：支持 `mock`（离线合成）/ `http`（接外部检测服务）双后端 |
| 全实体预判 | `Predictor`：按历史轨迹预测未来位置，带置信度与 `prediction_trusted` |
| 战斗评估 | `judge_combat`：按威胁比 / 血量 / 怪物档次给出 `fight / cautious_fight / retreat` |
| 动态心态 | `decide_mindset`：激进 / 平衡 / 保守，随场面变化 |
| 知识闭环 | `extract_tactics` + `apply_tactics`：战术带**适用条件**，命中才影响决策 |
| 拟人动作 | 移动抖动、随机停顿、安全区钳制、鼠标四角安全暂停 |
| BOSS 记忆 | 归纳移动模式 / 追踪距离，写回知识库 |
| 组队协同 | 识别队友、分工、保持距离 |
| 死亡复盘 | 只有 BOSS / 组队局才生成复盘，避免噪声 |
| 会话记忆 | 断点续玩、历史战绩 |
| 自动调参 | 按战斗统计自动微调阈值（阈值批次写入 `tuned_overrides.yaml`） |
| 自动汇报 | 生成 Markdown 报告，可选 Webhook 推送 |
| 视频学习 | 逐帧 VLM 提战术，去重后写入知识库 |

---

## 配置

改参数不用改源码，全部集中在这三处（**优先级从低到高**）：

1. `agent.py` 里的内置 `DEFAULT`（最后兜底）
2. [config.yaml](file:///workspace/Universal-Game-Framework/config.yaml)（全局配置）
3. `game_profiles/<游戏名>.yaml`（游戏档案，见 [game_profiles/](file:///workspace/Universal-Game-Framework/game_profiles)）

改完保存即**热加载**生效，不用重启。

### 常用环境变量

复制 [.env.example](file:///workspace/Universal-Game-Framework/.env.example) 为 `.env` 再填：

| 变量 | 作用 |
| --- | --- |
| `UGF_DRY_RUN=1` | 只记录动作，不碰真实键鼠（推荐先这样试） |
| `UGF_PERCEPTION_BACKEND` | `mock`（离线合成）/ `http`（接外部检测服务）/ `auto` |
| `UGF_PERCEPTION_URL` | 外部感知服务地址（留空则用 `127.0.0.1:<端口>/perceive`） |
| `AGENT_GAME` | 切换游戏档案，如 `space_invaders` |
| `LLM_API_URL` / `LLM_API_KEY` / `LLM_MODEL` | LLM 决策 |
| `VLM_API_URL` / `VLM_API_KEY` / `VLM_MODEL` | 视频学习的视觉模型 |
| `UGF_WEBHOOK_URL` | 报告推送地址 |

---

## 为什么这么轻

| 对比项 | 旧版（MCP 服务） | 现在（单文件 Agent） |
| --- | --- | --- |
| 文件数 | 100+ 个模块 / 文档 / 工具 | `agent.py` 一个文件 |
| 每回合开销 | MCP 子进程 + JSON-RPC 往返 6 次 | 进程内函数调用，**零 IPC** |
| 依赖 | flask / mcp / numpy / psutil / dotenv… | 核心只需 `pyyaml` + `requests`（缺失自动降级） |
| 离线可跑 | 需要装一堆包 | 只用标准库即可跑通 dry-run 全链路 |

细节设计：`requests` / `yaml` / `pyautogui` / `cv2` 全部**惰性 import**；
预判与战斗评估是**纯 math**，不引入 numpy。

---

## 测试

```bash
pip install pytest
python -m pytest tests/ -q      # 单元测试
python agent.py selftest        # 端到端离线自检
```

CI（[.github/workflows/ci.yml](file:///workspace/Universal-Game-Framework/.github/workflows/ci.yml)）会在 Python 3.11 / 3.13 上跑这两项。

---

## 目录结构

```
Universal-Game-Framework/
├── agent.py              # 全部能力都在这里（单文件 Agent）
├── config.yaml           # 全局配置（阈值 / 路径，可热加载）
├── game_profiles/        # 游戏档案：florr / demo_arcade / space_invaders
├── tests/                # 精简测试
├── ROADMAP.md            # 优化路线图（26 项技术优化清单）
├── requirements.txt      # 依赖（核心只有 pyyaml + requests）
├── pyproject.toml
├── Dockerfile
└── README.md
```

运行期会自动生成（已在 `.gitignore` 中忽略）：
`knowledge_md/`（经验本）、`run_logs/`（日志与报告）、`agent_state.json`、`session_history.json`。

---

## License

MIT，见 [LICENSE](file:///workspace/Universal-Game-Framework/LICENSE)。
