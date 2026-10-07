# 贡献指南

欢迎提交 Issue 与 PR！本文说明开发环境、双源工作流与质量门槛。

## 开发环境

```bash
git clone https://github.com/gupi-bill/Universal-Game-Framework.git
cd Universal-Game-Framework
pip install -r requirements.txt
pip install pytest pytest-cov ruff mypy pre-commit
pre-commit install          # 提交前自动跑全套体检
python agent.py selftest    # 应输出 19/19
```

无需图形环境：全部开发/测试都在 mock 感知 + dry-run 下离线进行。

## 双源工作流（重要）

仓库同时维护两种形态，**必须保持一致**（CI 会校验）：

| 形态 | 位置 | 用途 |
|---|---|---|
| 分片源 | `src/ugf/part*.py`（19 片） | 日常开发改这里 |
| 单文件 | `agent.py` | 对外分发形态，由分片构建生成 |

```bash
# 改了分片 → 重新生成单文件
python build.py agent

# 应急直接改了 agent.py → 反向同步分片
python build.py split

# 校验两者逐字节一致（CI 同款）
python build.py check-agent
```

分片按 agent.py 内的横幅注释（`# === N. 模块名 ===`）切分，拼接顺序即文件名排序，**不要手工改分片文件名**。

## 质量门槛（提交前全绿）

```bash
ruff check . && ruff format --check .   # lint + 格式（阻断）
python -m mypy agent.py                  # 类型（当前 0 error）
python -m pytest tests/ -q --cov=agent --cov-fail-under=70
python agent.py selftest                 # 端到端 19/19
python agent.py config-check             # 配置体检
python agent.py profile-check --all      # 游戏档案校验
python tools/secret_scan.py              # 密钥扫描
```

## 提交规范

- 提交信息：`type: 摘要`（feat / fix / docs / test / style / refactor），涉及路线图条目时标注编号，如 `（ROADMAP v2 #14）`
- 一个提交只做一件事；新能力必须带测试
- 核心数据流有类型契约（FramePayload / EntityPred / CombatEval / ActionDict），改动请保持 TypedDict 同步

## 添加新游戏

不需要改任何代码：复制 `game_profiles/_template.yaml` 按 [接入指南](docs/game-profile-guide.md) 填写，`profile-check --strict` 通过即可。

## 行为准则

保持技术讨论友善务实；不接收任何用于绕过在线游戏服务条款的功能请求（见 README 声明）。
