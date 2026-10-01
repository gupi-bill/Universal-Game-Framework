# P3 实证：能力可搬运（2026-10-01）

> 环境：Linux + Xwayland，无 YOLO 权重、无 `.env` 密钥
> 交付：`tools/install_mcp.py` 跨宿主支持、`tools/verify_portability.py`、
> `tests/test_install_mcp.py` +7 用例、本文档

## 1. 结论

> **P3 未被证伪。** 三个宿主、三种不同配置 schema、各 16 个工具，
> **工具集完全一致**，全程未改动本项目任何代码。

```
✓ workbuddy       16 个工具
✓ opencode        16 个工具
✓ vscode          16 个工具
✓ 一致性：3 个宿主的工具集完全相同
```

证伪条件是「换宿主需改本项目代码」—— 本轮一次都没改。

## 2. 为什么之前证不了：opencode 根本装不上

本机实测发现两个 bug：

| Bug | 现象 |
|-----|------|
| **只认 `opencode.json`** | 但 opencode 实际用 `opencode.jsonc` → `--list` 一直报「配置文件不存在」，用户会以为不支持 |
| **schema 用错** | 通用写法是 `mcpServers` + `command` 字符串 + `env`；opencode 实际是 `mcp` + `command` **数组** + `environment` + `type`/`enabled` |

第二点更隐蔽：即使路径对了，形状错了 opencode 也认不出来。
本机 `opencode.jsonc` 里已有一条 `cli-anything`，它的真实形状就是
`type:local` + 数组 command + `environment` —— 那是唯一可靠参照。

## 3. 改了什么

### TARGETS 支持多候选路径 + shape

```python
"opencode": {
    "paths": ["~/.config/opencode/opencode.jsonc",
              "~/.config/opencode/opencode.json"],
    "kind": "jsonc", "key": "mcp", "shape": "opencode",
}
```

`entry(dry, shape)` 按 shape 生成不同形状的注册项。

### JSONC 读写

`_strip_jsonc()` 做**词法级**注释剥离 —— 刻意保守：
只在字符串字面量之外识别 `//` 与 `/* */`，不去解析字符串内容。
（测试里专门锁了这条：`{"url": "http://x.com//path"}` 不能被吃掉。）

写回时存标准 JSON（opencode 接受），**但先备份** `*.bak` ——
直接改用户的 MCP 配置不能没有回头路。

## 4. verify_portability.py 做了什么

不是「检查配置写对了」，而是**按每份配置真的把服务拉起来**：

1. 读配置（JSON / JSONC）
2. 按各自 schema 取启动命令
   （数组 command vs 字符串 command+args；`environment` vs `env`）
3. 走 MCP stdio 协议真握手
   （`initialize` → `notifications/initialized` → `tools/list`）
4. 比对各宿主的工具集

用法：

```bash
python tools/verify_portability.py          # 人看
python tools/verify_portability.py --json   # 进 CI
```

退出码：0 = 至少两个宿主可用且一致；1 = 不一致。

## 5. 顺带修的一处门禁噪声

`install_mcp --check` 的 stdio 握手偶发失败（连跑 4 次里有 1 次红）。
原因是服务启动要 import mcp/numpy/flask，机器负载高时 90 秒不够或输出错乱。

**一个会误报的体检比没有体检更糟** —— 人会习惯性忽略红色。
`cmd_check` 现在重试两次再判失败；改完之后连跑 4 次全绿。

之前只在测试层加重试（`test_check_reports_healthy`），但门禁脚本
直接调 CLI，所以真正的修法在 `cmd_check` 里。

## 6. 局限（诚实记录）

- 只验到 **stdio 协议层**：工具清单一致。
  **没验「宿主 LLM 是否真的会用这些工具」** —— 那需要真实对话，
  是 P3 的下一步。
- codex 用 TOML，本项目只给指引不代写（避免写坏结构），
  因此不在自动探测范围内。
- 三个宿主都在**同一台机器、同一份源码**上跑。跨机器（换 OS、
  换 Python 版本）还没验。
- `claude-desktop` 本机没装（Linux 路径下无配置文件），未纳入。

## 7. 下一步

| 优先级 | 动作 | 目的 |
|-------|------|------|
| 1 | **真实对话验证**：在 opencode 里真的问一句，让 LLM 调 ugf 工具 | 从「工具一致」升级到「能力可用」 |
| 2 | 装 Claude Desktop 再验一个宿主 | 覆盖面从 3 到 4 |
| 3 | 跨机器验证（换 Python 版本 / 换 OS） | 排除「同机同源」的偶然 |
| 4 | 把 `verify_portability.py` 接进 CI | 防回归 |

第 1 条最关键 —— 现在证明的是「服务能起来、工具在」，
还没证明「换了宿主，LLM 用起来一样顺手」。


---

## 8. 第二层实证（2026-10-01 补）：宿主 LLM 真的会用

§4 的 `verify_portability.py` 只验到**协议层** —— 按各宿主配置把服务
拉起来、拿到工具清单。那证明「服务能起、工具在」。

但 P3 真正要问的是：**换了宿主，那套能力用起来一样顺手吗？**
只有宿主自己的 LLM 真的调一次才算数。

### 8.1 opencode 自己就报 connected

```
$ opencode mcp list
●  ✓ cli-anything connected
●  ✓ ugf       connected
    /home/g-bill/.workbuddy/binaries/python/envs/ugf/bin/python
    /home/g-bill/文档/Default Project/Universal-Game-Framework/mcp_server.py
└  2 server(s)
```

这不是我模拟的握手 —— 是 opencode 真实拉起进程并连接成功。
注意工具名被 opencode 自动加了 `ugf_` 前缀（`ugf_kb_list`），
说明宿主在正确处理多 MCP 服务的工具名冲突。

### 8.2 三个探针，全部取得 ugf 独有数据

新增 `tools/verify_live_usage.py`：调 `opencode run`，让宿主自己的
模型去调 ugf 工具，再检查返回内容是否**真的来自 ugf**。

| 探针 | 判据（ugf 独有） | 结果 |
|------|------------------|------|
| `ugf_guide` | 返回含 `kb_export` / `kb_import` | ✓ |
| `kb_list` | 返回含 `boss_behavior_log` / `learning_stats` | ✓ |
| `query_boss_history` | 返回 mantis 的行为记录 | ✓ |

**3/3 通过。**

其中 `query_boss_history` 返回的是真实知识库内容：

```
# BOSS 行为观察 — 20260922_013151
- 01:31:50 mantis(Super) 位置(1700.0,800.0) 预判(None,None) 决策=retreat
## 行为归纳（多次遭遇累计共性）
- mantis(Super): 直线移动；平均距离玩家约 784px；近距离接近 0 次
```

带时间戳、带位置坐标、带行为归纳 —— 这些不可能是模型编的。
反向测试也确认了：单独问 `kb_list` 时返回
`review_20260922_012845.md`、`s9_probe.md`、`smoke_s8.md`
等只存在于 ugf 知识库的文件名。

### 8.3 为什么不用 mock 掉 LLM

要验的就是「LLM 会不会用」。mock 掉 LLM 等于没验。

所以脚本在环境不具备（没装 opencode / 无可用模型 / ugf 未注册）
时返回**2（环境不具备）**，而不是 0（通过）——
门禁里「不具备」该被单独标记，混进「通过」就是自欺。

判据也刻意收窄：`expect_any` 只允许 ugf 独有的字符串，
单测里明确禁止 `tool` / `list` / `data` 这类通用词，
并要求判据必须能在本仓库 `knowledge_md/` 里找到出处。

### 8.4 P3 现在的完整状态

| 层次 | 验证内容 | 状态 |
|------|---------|------|
| 协议层 | 按各宿主配置拉起服务，工具集一致 | ✓ 3 宿主 × 16 工具 |
| 应用层 | 宿主 LLM 真的调用并取得真实数据 | ✓ 3/3 探针 |
| 跨机器 | 换 OS / 换 Python 版本 | ❌ 未验 |
| 长期 | 宿主升级后是否仍兼容 | ❌ 未验 |

**结论：P3 在「同一台机器、两个不同宿主」上成立。
跨机器与跨版本仍未验证。**
