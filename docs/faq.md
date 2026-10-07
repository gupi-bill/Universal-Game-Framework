# FAQ 常见问题

## 安装与运行

**Q：必须 pip install 吗？**
不必。核心离线链路（mock 感知 + dry-run 动作 + 全部决策逻辑）纯标准库即可跑：
`python agent.py selftest` 应输出 19/19。装了 `pyyaml` 才能读 config/档案，装了
`requests` 才有 LLM/Webhook/VLM——缺了自动降级并留日志，不会崩。

**Q：没有显示器 / 无头服务器能跑吗？**
能。`UGF_DRY_RUN=1 UGF_PERCEPTION_BACKEND=mock` 即全离线链路；这正是 CI 的跑法。
真机键鼠（pyautogui）才需要图形环境。

**Q：pip 安装后文件都写到哪里？**
知识库/日志/状态文件写 `UGF_HOME`（默认 `~/.ugf`），代码目录保持只读（ROADMAP #21）。
仓库内直接 `python agent.py` 则一切照旧写在仓库目录。

**Q：Windows 终端中文乱码？**
v3.1 已自动把 stdout/stderr 切到 UTF-8；仍乱码就用 `start.bat`（内置 chcp 65001）。
高 DPI 缩放（150%）下点击偏移也已处理（真实操作前自动声明 DPI 感知）。

## 感知与模型

**Q：真实感知怎么接？**
三选一：`http`（外部检测服务返回标准 payload）、`local`（本机 ONNX 推理，YOLOv8
导出格式）、`template`（OpenCV 模板匹配，零模型）。payload 契约见
`python agent.py guide` 的注意事项段。

**Q：LLM 用哪家的？**
任意 OpenAI 兼容端点：`.env` 里配 `LLM_API_URL / LLM_API_KEY / LLM_MODEL`。
没配就走规则兜底，功能不缺，只是少了"临场创造"。LLM 输出经过 schema 校验 +
一次修复重试，畸形输出不会污染动作（ROADMAP #8）。

**Q：预判的 model 字段是什么？**
`linear`（线性外推）/ `accel`(恒加速度) / `circular`(圆周) 三模型，`predictor.model: auto`
时按"回退一步"的实测残差自动选优（ROADMAP #7）。绕圈 BOSS 用 circular 明显更准。

## 知识库

**Q：知识库越跑越大怎么办？**
自动的：超过 `agent.kb_max_mb`（默认 50MB）归档最旧文件；视频学习战术自动合并近重复。
手动的：`python agent.py kb maintain`。

**Q：视频学习写进了垃圾战术怎么撤？**
`python agent.py kb history <文件名>` 查修订，`kb rollback <文件名>` 回到上一版。
所有写入前都有快照（ROADMAP #12）。另外多帧投票（`learn.min_votes`）已经过滤了
大部分单帧噪声。

**Q：自己调好的参数会被自动调参覆盖吗？**
把参数路径加进 `agent.tune_locked`（如 `[combat.retreat_ratio]`），自动调参会跳过
并在 `tune --status` 里留痕（ROADMAP #13）。

## 排障

**Q：出问题了先看哪里？**
`python agent.py logs --tail 50` 看文本日志；`logs --events --kind decision` 看结构化
决策流；`python agent.py mode` 确认当前运行模式；`run_logs/agent_snapshot.json` 是
最近回合快照。

**Q：外部服务（感知/LLM/Webhook）挂了会怎样？**
按 `resilience` 配置重试（线性退避），仍失败则降级：感知失败跳过本回合、LLM 失败
走规则兜底、Webhook 失败只写本地报告——主循环不崩，全程留日志（ROADMAP #5）。

**Q：怎么快速验证环境没坏？**
`python agent.py selftest`（19 项端到端断言）+ `python -m pytest tests/ -q`（110+ 单测）。

## 合规

**Q：能用来挂机网游吗？**
不能也不该。本项目仅用于本地 AI 智能体技术研究，请在本地/自建/已授权环境使用。
在 florr.io 等官方服务器运行 bot 违反游戏服务条款，可能导致封号。
