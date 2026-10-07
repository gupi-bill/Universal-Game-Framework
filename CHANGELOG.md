# Changelog

版本号遵循[语义化版本](https://semver.org/lang/zh-CN/)。格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。

## [3.1.0] - 2026-10-07

一次性落地 [ROADMAP.md](ROADMAP.md) 全部 26 项技术优化。测试 26 → 116，CI 覆盖率门槛 70%（实测 72%），mypy 0 error，ruff 全绿并阻断。

### 新增

- **感知后端插件化（#6）**：`local`（mss 抓屏 + ONNX Runtime 本地推理，YOLOv8 导出格式）、`template`（OpenCV 模板匹配，零模型）两种内置后端，依赖惰性导入、缺失给安装指引
- **预判模型升级（#7）**：恒加速度模型（中点时间戳最小二乘）+ 圆周运动模型（代数圆拟合 + 角速度外推），`predictor.model: auto` 按回测残差自动选优；恒加速轨迹 1.2s 外推误差从 1260px 降到 <1px
- **实体跟踪多因子匹配（#9）**：预测位置 + 方向一致性 + 角色跳变惩罚，交叉走位不串 ID；`match_max_dist` 防瞬移误挂
- **知识库 BM25 检索（#11）**：中文单字/二元组分词 + 经典 BM25 排序 + Top-N；权威战术文档永远置顶，防知识链断裂
- **知识库版本化（#12）**：写前快照、`kb history` / `kb rollback`，回滚可再回滚
- **调参审计与锁定（#13）**：`_audit` 记录旧值→新值→依据；`agent.tune_locked` 人工锁定
- **复盘可配置（#15）**：`review.enabled/trigger_boss/trigger_team/template`，BOSS 稀有度档改读档案不再硬编码；复盘文件带结构化字段
- **日志体系（#16）**：大小轮转（gzip）+ 超期清理真正落地；`events.jsonl` 结构化事件流（decision/death/tune/learn/session_end）；`logs` 命令
- **监控面板（#17）**：`agent.py panel`，纯标准库只读服务，2 秒轮询看回合/决策/威胁/事件/日志
- **状态版本化（#18）**：agent_state / session_history 带 schema_version，读取自动迁移，兼容旧格式
- **档案校验（#10）**：`profile-check [game] [--all] [--strict]`，按 _template 规范全字段校验
- **LLM 决策结构化（#8，P0）**：动作 schema 校验、错误回喂修复重试、prompt 下放档案、决策恒带 source 标签并进快照与报告
- **统一降级链（#5）**：`retry_call`（重试 + 线性退避 + 日志），感知/LLM/Webhook 全接入，`resilience` 配置段
- **pip 可安装化（#21）**：`UGF_HOME` 运行目录解析（环境变量 > 可写代码目录 > ~/.ugf）
- **zipapp 分发（#22）**：`build.py` 产出 dist/ugf.pyz（~180KB，裸机 Python 即跑）；v* tag 自动构建上传 Release
- **密钥防泄漏（#24）**：`tools/secret_scan.py`（7 类模式、防自命中、白名单）+ pre-commit + CI
- **故障注入测试（#20）**：19 项外部依赖故障/边界用例；覆盖率防倒退门槛进 CI
- **文档体系（#25）**：docs/architecture.md、docs/game-profile-guide.md、docs/faq.md、本 CHANGELOG；README 徽章
- **英文 README（#26）**：README_EN.md，中英互链
- **Windows 硬化（#19）**：UTF-8 控制台、DPI 感知声明、感知源分辨率→逻辑屏坐标换算、start.bat / start.ps1

### 变更

- **定位统一（#1，P0）**：仓库简介/README/config 三处对齐为"单文件游戏 Agent"，移除死 mcp 配置段
- **静态检查阻断（#3）**：ruff 规则集固化进 pyproject（E/F/W/B/SIM/C4/UP），修复全部存量告警，全库统一格式化；CI lint 独立 job 阻断
- **类型化（#4）**：FramePayload / EntityPred / CombatEval / ActionDict 四个 TypedDict 刻画核心数据流；决策链全量注解；mypy 宽松档非阻断进 CI
- **视频学习增强（#14）**：感知哈希跳过近重复帧（纯 Python PNG 解码兜底）、多帧投票（min_votes）、VLM 提示词可配置
- **Docker 现代化（#23）**：多阶段构建、非 root（uid 10001）、HEALTHCHECK、/data 卷、docker-compose 编排示例、CI 构建冒烟

### 修复

- kb 检索遇到非 UTF-8 文件崩溃（UnicodeDecodeError 未捕获）
- 8×8 平均哈希对纯色图恒为 0 导致不同颜色误判重复（退化分支编码平均灰度）

## [3.0.0] - 2026-09-07

- **单文件重写**：从 100+ 文件的 MCP 服务形态重构为单文件 `agent.py`（v3.0.0-single）
- 零 IPC：MCP 子进程 + JSON-RPC 往返 6 次/回合 → 进程内函数调用
- 惰性依赖：核心离线链路（mock 感知 + dry-run）纯标准库可跑
- 原 16 个 MCP 工具能力全部保留为子命令/内部方法
- 19 项端到端自检；Python 3.11/3.13 双版本 CI

[3.1.0]: https://github.com/gupi-bill/Universal-Game-Framework/releases/tag/v3.1.0
[3.0.0]: https://github.com/gupi-bill/Universal-Game-Framework/releases/tag/v3.0.0
