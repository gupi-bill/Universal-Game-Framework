# 🗺️ ROADMAP · 优化路线图

> 基于 v3.0.0-single 源码通读整理的技术优化清单，共 **26 项**，按主题分 6 类。
> 每项含【现状 → 优化 → 验收】三段，可直接作为工单逐项认领。
> 优先级：**P0** = 影响正确性/可信度，尽快做；**P1** = 明显提升质量；**P2** = 锦上添花。
> 按约定：不含上市、融资、真机实测等商务向事项，纯技术/工程/文档。
>
> **✅ 2026-10-07 状态：全部 26 项已落地（v3.1.0），逐项验收见 CHANGELOG.md 与各提交记录。**

---

## 一、架构与代码质量

### ✅ 1. 【P0】仓库定位统一：描述与 README 打架
- **现状**：仓库简介写"不是 Agent——装到别的 Agent 上的游戏能力包（MCP 服务）"，README 却写"一个能自己跑的单文件游戏 Agent，不需要装 MCP 服务"；`config.yaml` 里还残留 `mcp.streamable_http` 配置段。
- **优化**：三选一定调——(a) 单文件 Agent（改仓库简介，删 mcp 配置段）；(b) 双模式（README 增加"作为 MCP 服务接入"章节，把 mcp 配置真正实现）；(c) 恢复 MCP 入口为可选子命令 `agent.py serve`。
- **验收**：仓库简介、README 首屏、config.yaml 三处口径一致，无死配置。

### ✅ 2. 【P1】单文件与可维护性的两全：源码分片 + 构建合并
- **现状**：`agent.py` 已 2556 行，配置/知识库/预判/战斗/感知/动作/会话/学习/CLI 全部挤在一个文件，改动互相干扰的风险随体量上升。
- **优化**：保留"单文件"卖点不动——源码按模块拆分到 `src/ugf/`（config、kb、predict、combat、perception、action、session、learn、llm、cli），加一个 `build.py` 把分片拼接回根目录 `agent.py`；CI 校验"分片构建产物 == 仓库内 agent.py"。
- **验收**：`python build.py` 一键再生 agent.py；对外使用方式零变化。

### ✅ 3. 【P1】静态检查从"不阻断"变"阻断"
- **现状**：CI 里 `ruff check . || true`，形同虚设；代码无统一格式化基线。
- **优化**：修复存量 ruff 告警后去掉 `|| true`；加 `ruff format --check`；pyproject.toml 固化规则集（含行宽、import 排序）。
- **验收**：CI 静态检查红灯即失败；本地 `make lint` 一条命令过全检。

### ✅ 4. 【P2】类型注解补全 + mypy 渐进接入
- **现状**：部分函数有注解（`safe_float(v, default: float = 0.0)`），大量核心函数（`judge_combat`、`run_agent`、`llm_decide`）参数与返回值无类型。
- **优化**：先给数据流关键路径（帧 dict、实体 dict、决策 dict）定义 TypedDict，再逐模块补注解；mypy 以宽松档进 CI（不阻断 → 阻断两步走）。
- **验收**：核心决策链 100% 有类型；mypy 零新增告警。

### ✅ 5. 【P1】统一错误处理与降级链
- **现状**：感知超时、LLM 失败、webhook 推送失败各自 try/except，策略分散；`llm_decide` 失败落到 `fallback_decide`，但感知 http 失败、KB 文件损坏等路径的行为不统一。
- **优化**：定义异常分类（可重试/需降级/致命），每个外部依赖（感知、LLM、VLM、webhook）明确"重试次数 → 降级动作 → 上报方式"三段策略，集中写入 config.yaml 可调。
- **验收**：拔掉任一外部依赖，主循环不崩、有日志、有降级动作；新增对应故障注入测试。

---

## 二、感知与决策

### ✅ 6. 【P1】感知后端插件化
- **现状**：`Perception` 只有 mock / http 两种后端，http 依赖外部 YOLO 服务（FlorrVLM 时代的 perception_server），单文件版没带。
- **优化**：抽出 backend 接口（`frame() -> dict`），新增两种内置实现：(a) `mss 截图 + ONNX Runtime 本地推理`（可选依赖）；(b) `OpenCV 模板匹配`轻量后端；档案里声明用哪个。
- **验收**：`UGF_PERCEPTION_BACKEND=local` 无外部服务可跑通 dry-run 之外的真实链路。

### ✅ 7. 【P1】预判算法升级：从纯线性到多模型
- **现状**：`_Tracker.predict()` 基于最近帧速度做线性外推，`_linearity()` 衡量轨迹直线度打置信分；对绕圈、折返、加速的 BOSS 预判会飘。
- **优化**：加两个可选模型——(a) 恒加速度模型（二次外推）；(b) 周期运动检测（对 BOSS 历史轨迹做自相关，识别绕圈周期）；按实体类别在档案里配置用哪个，线性模型保留为兜底。
- **验收**：合成轨迹测试集（直线/加速/圆周/折返）上，新模型对非直线轨迹的 1.2s 预测误差显著低于线性基线。

### ✅ 8. 【P0】LLM 决策的结构化与降级
- **现状**：`llm_decide` 让 LLM 返回 dict，解析失败即整体落到 `fallback_decide`，中间没有修复尝试；prompt 硬编码在源码。
- **优化**：(a) 返回体做 JSON Schema 校验 + 一次自动重试（把校验错误回喂给 LLM）；(b) prompt 模板移到游戏档案可配置；(c) 决策链路记录"LLM/规则"来源标签进快照与复盘。
- **验收**：注入畸形 LLM 返回的测试通过；档案可覆盖 prompt；报告里能看出每回合决策来源。

### ✅ 9. 【P1】实体跟踪的 ID 稳定性
- **现状**：`_Tracker` 以 raw_id + 位置连续性匹配，`entity_timeout: 0.4` 抗漏检；检测器换 ID 或两实体交叉走位时容易串。
- **优化**：匹配打分加入"最近距离 + 速度方向一致性 + 稀有度类别一致"三因子；交叉走位场景用匈牙利算法做全局最优分配（纯 Python 可实现小规模版本）。
- **验收**：构造两实体交叉的合成帧序列测试，ID 不串。

### ✅ 10. 【P2】游戏档案 schema 校验命令
- **现状**：`_template.yaml` 是文档式模板，档案字段写错（拼写、类型、缺段）只在运行期以默认值静默兜底，问题难发现。
- **优化**：新增 `agent.py profile-check [game]`：按模板 schema 校验档案字段完整性与取值范围，输出错误/警告清单；CI 对 game_profiles/ 全量跑一遍。
- **验收**：故意写坏一个档案字段，profile-check 能点名报错。

---

## 三、知识库与学习闭环

### ✅ 11. 【P1】知识库检索升级：关键词 → 相关性排序
- **现状**：`_text_search` 是纯关键词包含匹配，命中一堆时无法排序，战术提取质量受限。
- **优化**：实现 BM25 打分排序（纯标准库可做，分词用简单 n-gram + 空白切分）；可选依赖启用 embedding 检索（本地小模型或 API），配置切换。
- **验收**：同一查询下 top3 命中率对比基线提升（用 seed 知识 + 人工标注小测集）。

### ✅ 12. 【P1】知识库版本化与可回滚
- **现状**：`kb_export/kb_import` 是整包备份，`kb_append` 写坏了（视频学习写入低质战术）只能手工修。
- **优化**：每次写入前自动留存轻量快照（仅 diff），`kb history <file>` 查看变更、`kb rollback <file> <rev>` 回滚；保留策略进 config。
- **验收**：写入→回滚→比对内容一致的测试通过。

### ✅ 13. 【P2】自动调参的审计与冲突处理
- **现状**：`auto_tune` 把批次结果写 `tuned_overrides.yaml`，与 config.yaml 的合并优先级隐式生效，人工改参后可能被覆盖且无痕迹。
- **优化**：调参写审计日志（旧值→新值→依据统计）；检测到人工修改过同一参数时告警并跳过；`auto_tuner_status` 展示最近 N 次变更。
- **验收**：人工锁定某参数后，auto_tune 不再覆盖且有日志说明。

### ✅ 14. 【P2】视频学习管线增强
- **现状**：`extract_frames` 固定 skip 抽帧，`_dedup_new` 按文本比例 0.75 去重；相似帧浪费 VLM 调用，战术质量靠 `_useful_tactic` 单层过滤。
- **优化**：(a) 抽帧后做感知哈希（纯 Python 平均哈希即可）跳过近重复帧；(b) 同一战术多帧投票，≥2 帧支持才入库；(c) VLM prompt 模板移入游戏档案。
- **验收**：同一段视频，VLM 调用次数下降且入库战术数不塌方（基准对比写进 learn 报告）。

### ✅ 15. 【P2】死亡复盘的触发与模板可配置
- **现状**：`should_review` 固定"BOSS/组队局才复盘"，逻辑写死在源码。
- **优化**：触发条件（死亡帧阈值、局类型、连死次数）与复盘模板全部下放游戏档案；复盘产出结构化字段（时间线、致命实体、当时套装）便于后续统计死因分布。
- **验收**：改档案即可开关复盘；复盘文件含结构化段落。

---

## 四、可靠性与运行时

### ✅ 16. 【P1】日志轮转落地 + 结构化日志
- **现状**：config 里 `logs.retention_days: 7`、`max_size_mb: 20` 已有配置项，`log()` 是简单追加写；轮转与压缩是否完整实现需要核对，且日志是纯文本难机器分析。
- **优化**：实现按大小轮转 + 超期清理（用配置值）；关键事件（决策、死亡、调参、学习入库）额外写 JSONL 结构化行；`agent.py logs --tail/--grep` 快捷查看。
- **验收**：长跑压测日志不超上限；JSONL 可被一行 python 统计出死亡率。

### ✅ 17. 【P2】可选轻量监控面板
- **现状**：单文件版砍掉了旧版 admin_panel，运行状态只能靠日志和 webhook。
- **优化**：`agent.py panel` 起一个标准库 http.server 面板（可选命令，不启动则零开销）：实时回合数、死亡数、当前决策来源、最近预判列表、日志尾；纯 HTML+轮询，不引第三方。
- **验收**：dry-run 长跑时浏览器打开面板数据在动；不启动 panel 时主循环无任何额外开销。

### ✅ 18. 【P1】状态文件 schema 版本化
- **现状**：`agent_state.json`、`session_history.json` 无版本字段，未来改结构会让老用户的断点续玩和历史战绩直接损坏。
- **优化**：两个文件加 `schema_version`，读取时按版本自动迁移；迁移函数集中在一个 `_migrations` 表。
- **验收**：拿旧格式文件启动，自动迁移且 resume_info 正常。

### ✅ 19. 【P2】跨平台兼容硬化（Windows 优先）
- **现状**：目标用户多在 Windows 上跑真实键鼠；代码里路径用 os.path 没问题，但控制台中文输出在 GBK 终端可能 UnicodeEncodeError，pyautogui 在 Windows 的 DPI 缩放、多显示器坐标也未处理。
- **优化**：stdout 重配置 UTF-8（带 fallback）；感知坐标统一"逻辑分辨率"概念，档案里声明并换算；提供 start.bat / start.ps1 一键脚本。
- **验收**：Windows 中文终端 GBK 编码下 selftest 全绿；150% 缩放屏幕坐标换算测试通过。

### ✅ 20. 【P1】故障注入测试与覆盖率门槛
- **现状**：21 个测试覆盖主干，但外部依赖故障（感知超时、LLM 畸形返回、KB 文件损坏、磁盘写入失败）与边界（空实体列表、零血量、负坐标）覆盖少；无覆盖率度量。
- **优化**：补 15+ 个故障/边界用例（monkeypatch 模拟）；CI 加 pytest-cov，先设"不低于当前值"的防倒退门槛，逐步提到 80%。
- **验收**：CI 出覆盖率报告；故意弄坏任一外部依赖，测试套能抓到。

---

## 五、分发与部署

### ✅ 21. 【P2】pip 可安装化（PyPI 发布）
- **现状**：只能 clone 仓库后 `python agent.py`；pyproject.toml 已具备但未定义入口。
- **优化**：pyproject 加 `[project.scripts] ugf = ...`，包结构支持 `pip install ugf`（或先 git+https 安装）；游戏档案与配置支持用户目录覆盖（`~/.ugf/`），安装后不依赖仓库目录。
- **验收**：干净虚拟环境 `pip install .` 后，任意目录下 `ugf selftest` 通过。

### ✅ 22. 【P2】zipapp 单文件分发
- **现状**："单文件"指源码组织，用户仍需装 Python + 拉仓库。
- **优化**：`build.py` 追加 zipapp 目标，产出 `ugf.pyz`（标准库 zipapp，Python 3.11+ 直接 `python ugf.pyz` 运行）；Release 附件自动上传（CI workflow）。
- **验收**：只装 Python 的裸机下载 pyz 即可 selftest。

### ✅ 23. 【P2】Docker 镜像优化与 compose 示例
- **现状**：Dockerfile 存在但较朴素；感知服务、面板端口未编排。
- **优化**：多阶段构建 + python-slim 基础镜像 + 非 root 用户 + HEALTHCHECK（跑 selftest）；提供 docker-compose.yml 示例（agent + 外部感知服务两个 profile）；CI 加镜像构建冒烟。
- **验收**：`docker compose up` 一键起 dry-run 全链路；镜像体积对比优化前下降。

### ✅ 24. 【P2】密钥安全防线
- **现状**：`.env.example` 提供了模板，但没有机制阻止用户把真 `.env`、API key 误提交（.gitignore 挡住了 .env，但 key 也可能写进 config.yaml 或档案）。
- **优化**：pre-commit 钩子接 gitleaks（或纯 Python 的密钥正则扫描）；CI 加同样的扫描步骤；config 加载时对疑似明文 key 打告警建议改环境变量。
- **验收**：提交含假 key 的分支，pre-commit 与 CI 都能拦截。

---

## 六、文档与生态

### ✅ 25. 【P1】文档体系补全
- **现状**：README 质量高但独苗一份；无架构图、无"如何写一个新游戏档案"教程（_template.yaml 有注释但缺流程文档）、无 FAQ、无 CHANGELOG。
- **优化**：新增 `docs/`：architecture.md（分层图 + 数据流）、game-profile-guide.md（从零接入一款新游戏的完整步骤）、faq.md、CHANGELOG.md（从 git log 整理 v3.0.0 以来的变更）；README 顶部加徽章（CI 状态 / Python 版本 / License）。
- **验收**：新人只按 game-profile-guide 能在 30 分钟内给一款自制小游戏写出可跑档案（以 demo_arcade 为对照）。

### ✅ 26. 【P2】英文 README 与国际化
- **现状**：README 纯中文；项目本身（游戏 Agent、MIT 协议）对海外开发者有吸引力，旧版 FlorrVLM 时期曾有海外社区关注。
- **优化**：补 README_EN.md（不是逐字翻译，按海外读者习惯重排：先 demo GIF/截图位、再 quickstart）；README 顶部互链语言切换；代码内 docstring 关键段落补英文。
- **验收**：英文 README 覆盖 quickstart / commands / profile / architecture 四块，术语与代码一致。

---

## 附：完成情况

全部 26 项已按编号逐项独立提交（git log 可追溯，提交信息均带 ROADMAP 编号）。
验证基线：116 项测试全绿 · selftest 19/19 · ruff check/format 阻断级全绿 ·
mypy 0 error · 覆盖率 72%（门槛 70%）· 分片构建逐字节一致 · zipapp 冒烟通过 ·
密钥扫描干净 · profile-check 三档案全过。

---

*本清单由千问工作助理基于 2026-10-07 main 分支源码通读整理生成。*
