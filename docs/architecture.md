# 架构总览

> 本文解释 Universal-Game-Framework（v3.1+）的分层结构、数据流与关键设计决策。
> 所有能力都在单文件 `agent.py` 中，本文的"模块"指文件内以注释横幅划分的逻辑段。

## 一、整体数据流

```
                ┌─────────────────────────────────────────────────┐
                │                 run_agent 主循环                  │
                └─────────────────────────────────────────────────┘
   每回合：
   ① 感知        Perception.frame() ──► mock / http / local(ONNX) / template(cv2)
                     │  统一 payload: {player, entities, teammates, afk_popup}
                     ▼
   ② 预判        Predictor.update(entities) ──► _Tracker × N
                     │   多因子匹配（预测位置+方向一致性+角色惩罚，#9）
                     │   三模型外推 linear/accel/circular + auto 回测选优（#7）
                     ▼
   ③ 评估        CombatEvaluator.evaluate(player, preds, teammates)
                     │   威胁求和 → threat_ratio → decision/mindset/套装（防抖缓存）
                     ▼
   ④ 知识        kb_search("战术"/"boss") ──► BM25 排序 + 权威文档置顶（#11）
                     │   extract_tactics：带适用条件的战术才参与决策
                     ▼
   ⑤ 决策        llm_decide（schema 校验+修复重试，#8）
                     │   失败降级 fallback_decide（规则+知识加权），恒带 source 标签
                     ▼
   ⑥ 动作        scale_coords（#19）→ clamp_to_safe_zone → apply_jitter
                     │   game_action / switch_set（dry-run 只落盘不碰键鼠）
                     ▼
   ⑦ 记忆        BOSS 行为采样→归纳写库 ｜ 死亡复盘（触发条件可配，#15）
                     │   会话状态 schema 版本化（#18）｜ events.jsonl 事件流（#16）
                     ▼
   ⑧ 汇报        write_snapshot → generate_report → webhook（统一降级链，#5）
                     │   auto_tune 按战损微调阈值（审计+锁定，#13）
                     ▼
   ⑨ 学习        learn_from_video：抽帧→哈希跳帧→VLM→多帧投票→去重入库（#14）
```

## 二、模块地图（agent.py 内部段落）

| 段 | 模块 | 职责 | 关键入口 |
|---|---|---|---|
| 0 | 轻量工具 | .env 加载 / 安全转换 / 日志+轮转 / 事件流 / 降级重试 | `log` `log_event` `retry_call` |
| 1 | 配置 | 四级优先合并、热加载、派生路径、运行目录解析 | `reload_config` `cfg_get` `_resolve_base_dir` |
| 2 | 知识库 | Markdown 经验库：检索/写入/快照回滚/压缩归档 | `kb_search` `kb_write` `kb_rollback` |
| 3 | 预判 | 实体跟踪 + 三模型运动外推 + 置信度 | `Predictor` `_Tracker.predict` |
| 4 | 战斗评估 | 威胁比 → 打/谨慎/跑 + 心态 + 组队协同 + 走位 | `judge_combat` `retreat_position` |
| 5 | 知识闭环 | 战术条件解析、知识闸门、决策影响 | `extract_tactics` `decide_action` |
| 6 | 感知 | 四后端插件化，统一 payload | `Perception` |
| 7 | 动作 | 拟人化执行、dry-run、DPI/坐标换算 | `game_action` `switch_set` |
| 8 | 会话 | 断点续玩、战绩历史（版本化迁移） | `session_load` `_read_history` |
| 9 | 复盘 | 触发判定、模板渲染、BOSS 行为归纳、快照 | `review_round` `analyze_boss_behavior` |
| 10 | 汇报 | Markdown 报告、Webhook、局中进度 | `generate_report` `notify` |
| 11 | 调参 | 战损驱动阈值微调 + 审计 + 锁定 | `auto_tune` |
| 12 | 视频学习 | 抽帧、感知哈希、VLM 提战术、投票、去重 | `learn_from_video` |
| 13 | LLM 大脑 | 结构化决策 + 修复重试 + 规则兜底 | `llm_decide` `fallback_decide` |
| 14 | 主循环 | 九步编排 + 安全暂停 + 收尾 | `run_agent` |
| 14a | 面板 | 只读监控 HTTP 服务（可选启动） | `run_panel` |
| 15 | CLI | argparse 子命令 + 端到端自检 | `main` `selftest` |
| 15a | 档案校验 | 游戏档案静态检查 | `profile_check` |

## 三、配置优先级（低 → 高）

```
agent.py 内置 DEFAULT
  < config.yaml            （全局通用项）
  < game_profiles/<游戏>.yaml （游戏专属项）
  < ~/.ugf 或 UGF_HOME 下同名文件（安装版个人配置，#21）
  < tuned_overrides.yaml   （自动调参写入，可被 agent.tune_locked 锁定）
```

- 三个文件任一 mtime 变化即热加载（`reload_if_changed`），无需重启。
- 环境变量最高优先：`AGENT_GAME` / `UGF_DRY_RUN` / `UGF_PERCEPTION_BACKEND` / `UGF_HOME` 等。

## 四、类型契约（#4）

核心数据流有 4 个 TypedDict 定义（见 agent.py 头部）：

| 类型 | 描述 |
|---|---|
| `FramePayload` | 感知帧：player / entities / teammates / afk_popup / error |
| `EntityPred` | 预判实体：坐标、速度、置信度、model（linear/accel/circular） |
| `CombatEval` | 评估结果：decision / recommended_set / mindset / threat_ratio |
| `ActionDict` | 动作：action ∈ move/attack/defend/synthesize/idle，恒带 source ∈ llm/kb/rule |

## 五、可靠性设计

- **降级链（#5）**：感知/LLM/Webhook 全部走 `retry_call`（重试+线性退避+日志），最终失败有明确降级动作，主循环永不因外部依赖崩溃。
- **数据自愈（#18）**：状态文件损坏 → 回落默认值；旧版本文件 → 自动迁移。
- **知识可回滚（#12）**：每次写入前快照，`kb rollback` 一条命令撤销。
- **安全边界**：dry-run 全局开关；鼠标甩屏幕角落即暂停；动作白名单；文件名消毒防路径穿越；密钥扫描进 CI（#24）。

## 六、运行期产物（均不入 git）

```
$UGF_HOME（默认=仓库目录）/
├── knowledge_md/<game>/    经验库（战术/复盘/BOSS 记忆/.history 快照）
├── run_logs/               agent_*.log（轮转压缩）/ events.jsonl / 报告 / 快照
├── video_frames/           视频学习临时帧
├── agent_state.json        会话状态（schema_version）
├── session_history.json    战绩历史（schema_version）
└── tuned_overrides.yaml    自动调参覆盖（含 _audit 审计）
```

---

*相关文档：[新游戏接入指南](game-profile-guide.md) ｜ [FAQ](faq.md) ｜ [优化路线图](../ROADMAP.md)*
