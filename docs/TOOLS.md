# MCP 工具速查

## 知识库类（6 个）

| 工具 | 参数 | 返回值 | 说明 |
|------|------|--------|------|
| `kb_list` | `game_name?` | JSON 数组 `["file.md",...]` | 列出知识库文档名 |
| `kb_search` | `keyword`, `game_name?` | 片段文本 | 关键词搜索知识库 |
| `kb_write` | `filename`, `markdown_content`, `game_name?` | `已写入知识库: <路径>` 或错误 | 覆盖写入 |
| `kb_append` | `filename`, `markdown_content`, `game_name?` | `已追加到知识库: <路径>` 或错误 | 追加 |
| `kb_export` | — | 备份路径 | 打包 `tar.gz` |
| `kb_import` | `backup_path` | 恢复结果文本 | 从备份恢复 |

## 感知与预判（3 个）

| 工具 | 参数 | 返回值 | 说明 |
|------|------|--------|------|
| `perceive_game` | — | JSON 字符串 | 截图+检测，返回实体列表 |
| `predict_all_entities` | — | JSON 数组 | 按威胁降序的实体预判 |
| `reset_predictor` | — | 确认文本 | 清空预判历史帧 |

## 动作（2 个）

| 工具 | 参数 | 返回值 | 说明 |
|------|------|--------|------|
| `game_action` | `action_type`, `x?`, `y?` | `已执行/思考中/未知...` | 执行 move/attack/defend/idle/synth |
| `switch_set` | `set_name` | `已切换套装: ...` 或未知提示 | 切换战斗套装 |

## 辅助（5 个）

| 工具 | 参数 | 返回值 | 说明 |
|------|------|--------|------|
| `handle_afk` | — | 指引文本 | AFK 验证流程触发 |
| `query_boss_history` | `boss_name?` | 习惯文本或无 | 查 BOSS 历史 |
| `clean_cache` | `target?` | 清理结果 | all/predict/frames |
| `switch_tactic` | `tactic_file` | 切换成功 | 指定当前战术文档 |
| `ugf_guide` | `section` | 手册文本 | all/quick/tools/chains/notes |

## 环境开关

| 变量 | 值 | 效果 |
|------|-----|------|
| `UGF_DRY_RUN` | 1 | 不动键鼠，所有动作只看日志 |
| `UGF_PERCEPTION_BACKEND` | mock/http/auto | 感知后端选择 |
| `FLORR_VECTOR_SEARCH` | 0/1 | 向量检索开关 |
