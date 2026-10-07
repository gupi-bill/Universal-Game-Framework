# 🗺️ ROADMAP v2 · 第二波优化路线图（目标 v3.2.0）

> 基于 v3.1.0（第一波 26 项已全部落地）源码现状整理的第二批技术优化清单，共 **22 项**，按主题分 5 类。
> 每项含【现状 → 优化 → 验收】三段，按编号逐项认领、独立提交。
> 优先级：**P0** = 影响可用性/正确性；**P1** = 明显提升质量；**P2** = 锦上添花。
> 依旧不含商务向事项，纯技术/工程/文档。第一波清单见 [ROADMAP.md](ROADMAP.md)。

---

## 一、观测与诊断

### 1. 【P1】doctor 环境体检命令
- **现状**：环境是否可跑真机链路（pyautogui/显示服务/感知服务可达/密钥配置/UGF_HOME 可写/档案合法性）要挨个手工试，出问题靠猜。
- **优化**：新增 `agent.py doctor [--game NAME]`：逐项体检 Python 版本、可选依赖清单（yaml/requests/pyautogui/cv2/mss/onnxruntime 装了没）、UGF_HOME 可写性、当前档案 profile-check、感知服务连通性（http 后端时 GET 一次）、LLM/VLM/Webhook 配置状态（只报有无不报值），输出 ✓/✗/⚠ 清单与建议，全过退出码 0。
- **验收**：干净环境与完整环境各跑一次，缺什么明确指出装什么；有致命项时退出码 1。

### 2. 【P1】bench 分段耗时基准
- **现状**：主循环快不快、慢在哪一段（感知/预判/评估/决策/动作）没有度量，性能回退无感知。
- **优化**：新增 `agent.py bench [--rounds 200]`：mock+dry-run 下跑 N 回合，`time.perf_counter` 分段计时，输出各段 avg/p50/max 表格 + 回合吞吐；结果写一条 `bench` 事件进 events.jsonl，便于跨版本对比。
- **验收**：bench 200 回合输出五段耗时表；events.jsonl 出现 kind=bench 记录。

### 3. 【P2】logs --stats 事件流统计
- **现状**：events.jsonl 有决策/死亡/调参/学习/会话事件，但只能 tail 看原始行，没有聚合视角。
- **优化**：`agent.py logs --stats [--kind K]`：统计决策动作分布、决策来源占比（llm/kb/rule）、死亡率（death/decision 事件比）、调参次数、学习入库量、会话数与总回合，一屏输出。
- **验收**：dry-run 20 回合后 --stats 输出动作分布与来源占比，数字与事件行数一致。

### 4. 【P2】面板 /healthz 端点
- **现状**：panel 只有 / 与 /api/state；容器编排/负载均衡缺标准探针端点。
- **优化**：GET /healthz 返回 `{"status":"ok","version":...,"ts":...}`（200）；Dockerfile 与 compose 的 healthcheck 说明更新为可选用 healthz。
- **验收**：panel 启动后 /healthz 返回 200 与版本信息；未知路径仍 404。

---

## 二、运行时增强

### 5. 【P0】按游戏隔离会话状态
- **现状**：agent_state.json / session_history.json 全局一份，`AGENT_GAME` 切换游戏后，续玩提示、累计场次、战绩历史全部串到别的游戏头上（resume_info 会拿 A 游戏的进度提示 B 游戏续玩）。
- **优化**：两个状态文件改为按 game 分区存储（顶层 `{"schema_version":2,"games":{<game>:{...}}}`），读取时用 v1→v2 迁移函数把旧全局数据归到当前档案名下；session_load/save/_read_history/_history_append 全走分区；`session` 命令显示当前游戏并可 `--all` 汇总。
- **验收**：同一目录先后以两个 AGENT_GAME 各跑一局，互不串数据；旧 v1 文件自动迁移不丢历史；新增迁移测试。

### 6. 【P1】run --hours 时长上限 + --resume 断点恢复
- **现状**：长跑只有 --rounds 一种停法；进程被杀后回合计数、当前套装、调参冷静期全丢（会话只记"上次打了多少回合"）。
- **优化**：`--hours N` 到时优雅收尾（走既有 finally 汇报链）；每 30 回合把循环现场（rounds/deaths/current_set/death_streak/cooldown 余量）落盘 `run_logs/loop_checkpoint.json`，`run --resume` 存在检查点时从现场继续（套装状态同步恢复），正常收尾后清除检查点。
- **验收**：--hours 0.01 能按时收尾；kill 后 --resume 回合数接续；收尾后无残留检查点；测试覆盖落盘/恢复/清除。

### 7. 【P1】brief 开局侦察报告
- **现状**：开跑前想知道"这个游戏档案什么配置、知识库攒了多少经验、历史战绩如何、调参状态"要敲四五个命令。
- **优化**：新增 `agent.py brief [--game NAME]`：一屏聚合——档案概要（稀有度档/套装/战术数）、运行模式（mode 同款）、知识库统计（文件数/体积/最近修订）、历史战绩摘要、调参状态与锁定项、doctor 的致命项提示（复用其检查）。
- **验收**：brief 输出六个板块且与各自单命令数据一致；档案不存在时明确报错。

### 8. 【P2】capture_region 字符串简写
- **现状**：`perception.capture_region` 只接受 mss 风格 dict（left/top/width/height），配置文件里写起来啰嗦。
- **优化**：兼容 `"x,y,w,h"` 字符串简写（解析为 dict，非法格式报错并回落全屏），dict 写法保持兼容；config.yaml 注释与文档同步。
- **验收**：字符串/dict/非法值三种输入的解析测试；非法值留日志回落。

---

## 三、知识库与档案

### 9. 【P2】kb boss --top N 危险度排行
- **现状**：boss_behavior_log.md 只增不排，`kb boss` 按名字过滤，无法回答"哪个 BOSS 杀我最多"。
- **优化**：解析 BOSS 行为日志与复盘文件的结构化字段（killer_entities），按 遭遇次数/致死次数/最近遭遇 聚合排序，`kb boss --top 5` 输出排行榜表格。
- **验收**：构造多条日志后排行正确；无数据时友好提示。

### 10. 【P2】kb stats 知识库统计
- **现状**：知识库规模与变动情况（多少文件、多大、多少历史修订、回滚过几次）无处可看，体积维护只有超限才触发。
- **优化**：`agent.py kb stats [--all]`：当前游戏分区的文件数、总体积、最大文件 Top5、.history 修订总数、最近一次学习入库时间（events.jsonl learn 事件）、归档目录体积。
- **验收**：数字与实际目录一致；空库输出零值不报错。

### 11. 【P1】游戏档案 extends 继承
- **现状**：同系列游戏（如 florr 变体）要整份复制档案改几个字段，稀有度/威胁表大量重复，改公共项要改 N 份。
- **优化**：档案顶层支持 `extends: <另一档案名>`：加载时先读父档案再深合并子档案（子覆盖父，列表整体替换不做拼接，避免语义歧义）；递归深度上限 4 防环，检测到环报错；profile-check 对合并后的生效配置校验，并提示继承链。
- **验收**：子档案只写差异字段即可通过 profile-check 且 cfg 合并正确；环继承报错；新增继承/覆盖/防环测试。

### 12. 【P2】replay 对局回放（文本时间线）
- **现状**：events.jsonl 记录了每回合决策，但没有"像看录像一样按时间轴回看一局"的入口。
- **优化**：`agent.py replay [--last | --session N] [--tail M]`：把指定会话时间窗内的 decision/death/tune/learn 事件重排成可读时间线（回合、动作、来源、决策、心态、死亡点标注），死亡回合高亮前后 3 回合上下文。
- **验收**：dry-run 一局后 replay --last 输出完整时间线且死亡点有上下文；无事件时提示先跑一局。

---

## 四、工程化

### 13. 【P1】pre-commit 全家桶
- **现状**：.pre-commit-config.yaml 只有 secret-scan 一个钩子；ruff/mypy/分片一致性都要等 CI 才暴露。
- **优化**：补齐 local hooks：ruff check（阻断）、ruff format（自动修）、mypy agent.py（阻断，当前 0 error 可直接收紧）、build.py check-agent（阻断）、secret_scan（保留）；顺序按"快→慢"排。
- **验收**：pre-commit run --all-files 全绿；故意弄脏一处能拦住。

### 14. 【P1】config-check 配置体检命令
- **现状**：profile-check 只管游戏档案；config.yaml / tuned_overrides.yaml 写错键名只会静默用默认值，范围写飞了（如 confidence_threshold: 5）运行时才出诡异行为。
- **优化**：新增 `agent.py config-check`：对照 DEFAULT 全键表校验两份文件——未知键 WARN（可能是拼写错误）、类型不符 ERROR、有安全区间的键越界 ERROR/WARN（复用调参钳制区间与端口区间）；输出与 profile-check 同款清单与退出码，CI 加入该步骤。
- **验收**：故意写错键名/类型/越界各能点名；干净配置通过；CI 跑通。

### 15. 【P1】随机轨迹 fuzz 属性测试
- **现状**：单测都是构造好的确定性用例；真实感知数据千奇百怪（漏检、抖动、坐标跳变、字段缺失），预判/评估/决策链在随机脏数据下的健壮性没有系统性验证。
- **优化**：tests/test_fuzz.py：固定随机种子生成 200 组随机轨迹序列（含 NaN/None/负坐标/字符串数字/字段缺失）喂 Predictor.update + all_entities + judge_combat + fallback_decide 全链，断言不抛异常且输出契约字段齐全、数值有限；再随机生成配置扰动（阈值极端值）跑 decide_action 链。
- **验收**：fuzz 测试进 CI 稳定绿；故意注入一个会崩的输入能被抓到（开发期验证）。

### 16. 【P2】dependabot 依赖更新
- **现状**：requirements/pyproject/Actions 版本靠人肉盯，安全补丁滞后。
- **优化**：.github/dependabot.yml：pip（requirements.txt + pyproject）与 github-actions 两个 ecosystem，周更，分组 minor/patch，忽略 major 自动合并（人工审）。
- **验收**：文件格式合法（CI yaml lint 或本地 python -c yaml.safe_load 验证）。

### 17. 【P2】社区文件三件套
- **现状**：无 CONTRIBUTING、无 issue 模板、无 SECURITY 政策，外部贡献者无从下手。
- **优化**：CONTRIBUTING.md（开发环境/双源工作流/提交规范/测试要求/行为准则）；.github/ISSUE_TEMPLATE 的 bug_report.yml 与 feature_request.yml（表单式）；SECURITY.md（漏洞私下报告渠道、支持版本、响应时限承诺）。
- **验收**：三件套入库，README 增加入口链接。

### 18. 【P1】Release 产物加固 + PyPI 发布骨架
- **现状**：tag 只传 ugf.pyz 一个产物，无校验和；pyproject 完备但从未发布 PyPI。
- **优化**：release.yml 增加 SHA256SUMS 生成与上传；新增 workflow_dispatch 手动触发的 pypi-publish.yml（build → twine upload，走 PyPI Trusted Publishing 的 OIDC 配置骨架，secrets 未配时明确失败提示）；文档写明首次发布步骤。
- **验收**：本地跑 release 构建步骤产物含校验和文件；pypi workflow yaml 合法且默认不自动触发。

---

## 五、体验与文档

### 19. 【P2】英文使用手册（AGENT_LANG）
- **现状**：README 已双语，但 `agent.py guide` 手册与 CLI help 纯中文，海外用户接不上。
- **优化**：新增 GUIDE_EN 常量；`AGENT_LANG=en` 或 `guide --en` 输出英文手册；argparse 的 epilog 示例双语。命令输出主体（日志/报告）保持中文不动（改造成本高、非目标用户刚需），文档说明该边界。
- **验收**：AGENT_LANG=en python agent.py guide 输出英文；默认仍中文；测试覆盖两分支。

### 20. 【P2】session --report 战绩统计报告
- **现状**：session 命令只有三行摘要；历史战绩（每局回合/死亡）存了却没分析。
- **优化**：`session --report`：按游戏分组统计——局数、总/均回合、总/均死亡、死亡率（死亡/回合）、最佳与最差一局、最近 7 天趋势（按日聚合），Markdown 表格输出并可选落盘 run_logs/session_report.md。
- **验收**：构造多局历史后统计数字正确；空历史友好提示。

### 21. 【P2】Windows 常驻运行指南
- **现状**：start.bat 解决"手动跑一次"，7×24 驻守（本项目的核心场景）没有官方指引，用户各显神通。
- **优化**：docs/windows-service.md：三种方案步骤化——任务计划程序（开机自启+崩溃重拉，含 schtasks 命令与 XML 模板）、NSSM 注册为服务、PowerShell 循环守护脚本；含日志轮转与 UGF_HOME 说明、卸载步骤。Linux 侧补 systemd unit 示例（仓库已有 florr 时代的 service 样例可参考重写）。
- **验收**：文档入库且命令可复制执行；README/FAQ 增加链接。

### 22. 【P1】demo 走查脚本 + 文档收尾
- **现状**：新人 5 分钟上手路径散落在 README/FAQ/guide 三处，没有一条"照做必成功"的演示动线；docs 缺 v3.2 新命令说明。
- **优化**：docs/demo.md：一条完整走查动线（selftest → dry-run 20 回合 → panel 面板 → logs --stats → brief → replay --last → report），每步含预期输出摘要；tools/demo.sh 一键串跑（离线、幂等）；全部 v3.2 新命令同步进 GUIDE、README 命令表、docs/architecture.md 模块地图；版本号升 3.2.0，CHANGELOG 补 v3.2.0 段。
- **验收**：bash tools/demo.sh 在干净环境一次跑通；文档三处命令表与实际 CLI 一致。

---

## 附：认领方式

按编号点单（例："做 1、5、11"），每项独立可交付、独立提交。
P0 共 1 项（#5 按游戏隔离状态——切游戏串数据是正确性问题），建议最先做。

---

*本清单由千问工作助理基于 v3.1.0 源码现状整理生成（2026-10-07）。*
