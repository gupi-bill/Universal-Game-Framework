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
