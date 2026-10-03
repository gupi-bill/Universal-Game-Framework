# MCP 工具请求示例

每个工具一条完整 JSON 请求-响应示例。在 dry-run 下均可跑通。

---

## 知识库类

### kb_list
```jsonc
// 请求
{"tool": "kb_list", "arguments": {"game_name": "florr"}}
// 响应
["combat_notes.md", "boss_behavior_log.md", "player_tactics.md"]
```

### kb_search
```jsonc
// 请求
{"tool": "kb_search", "arguments": {"keyword": "mantis", "game_name": "florr"}}
// 响应（命中片段）
"## 螳螂（Mantis）\nSuper 档 BOSS，第一阶段飞扑，第二阶段旋风刀。躲在地下可规避。"
```

### kb_write
```jsonc
// 请求
{"tool": "kb_write", "arguments": {"filename": "mantis_notes.md", "markdown_content": "# Mantis\nSuper BOSS...\n", "game_name": "florr"}}
// 响应
"已写入知识库: /home/g-bill/文档/.../knowledge_md/florr/mantis_notes.md (32 字符)"
```

### kb_append
```jsonc
// 请求
{"tool": "kb_append", "arguments": {"filename": "mantis_notes.md", "markdown_content": "\n第二阶段：旋风刀，注意绕背。\n", "game_name": "florr"}}
// 响应
"已追加到知识库: /home/g-bill/文档/.../knowledge_md/florr/mantis_notes.md"
```

### kb_export
```jsonc
// 请求
{"tool": "kb_export", "arguments": {}}
// 响应
"已备份到: /home/g-bill/文档/.../.kb_backup_20261002.tar.gz"
```

### kb_import
```jsonc
// 请求
{"tool": "kb_import", "arguments": {"backup_path": ".kb_backup_20261002.tar.gz"}}
// 响应
"恢复完成：12 个文件已恢复"
```

---

## 感知与预判类

### perceive_game
```jsonc
// 请求
{"tool": "perceive_game", "arguments": {}}
// 响应
{
  "player": {"alive": true, "hp": 100, "max_hp": 100, "x": 960, "y": 540, "power_score": 120, "petal_set": "combat", "talent": "none"},
  "entities": [
    {"raw_id": "hornet", "rarity": "Common", "x": 400, "y": 300},
    {"raw_id": "ladybug", "rarity": "Unusual", "x": 700, "y": 420}
  ],
  "teammates": [],
  "afk_popup": false,
  "_backend": "mock",
  "_mock": true
}
```

### predict_all_entities
```jsonc
// 请求
{"tool": "predict_all_entities", "arguments": {}}
// 响应（至少需要 3 帧历史帧，不足返回 insufficient_data）
[
  {"raw_id": "mantis", "rarity": "Super", "category": "boss", "threat_score": 400, "x_now": 800, "y_now": 600, "x_predict": 850, "y_predict": 620, "vx_per_sec": 50, "vy_per_sec": 20, "confidence": 0.87, "prediction_trusted": true},
  {"raw_id": "hornet", "rarity": "Common", "category": "normal", "threat_score": 15, "x_now": 400, "y_now": 300, "x_predict": 410, "y_predict": 305, "vx_per_sec": 10, "vy_per_sec": 5, "confidence": 0.92, "prediction_trusted": true}
]
```

### reset_predictor
```jsonc
// 请求
{"tool": "reset_predictor", "arguments": {}}
// 响应
"已清空预判历史帧。"
```

---

## 动作类

### game_action
```jsonc
// 请求（移动）
{"tool": "game_action", "arguments": {"action_type": "move", "x": 960, "y": 540}}
// 响应
"动作执行成功: move (960,540)"

// 请求（攻击）
{"tool": "game_action", "arguments": {"action_type": "attack"}}
// 响应
"动作执行成功: attack"
```

### switch_set
```jsonc
// 请求
{"tool": "switch_set", "arguments": {"set_name": "combat"}}
// 响应
"已切换套装: combat (按键 1)"

// 请求（非法名）
{"tool": "switch_set", "arguments": {"set_name": "invalid"}}
// 响应
"未知套装: invalid，可选 combat/tank/retreat/chase/team"
```

---

## 辅助类

### handle_afk
```jsonc
// 请求
{"tool": "handle_afk", "arguments": {}}
// 响应
"AFK 弹窗处理流程已触发：请结合 perceive_game 返回的弹窗坐标，用 game_action(move) 完成点击验证。"
```

### query_boss_history
```jsonc
// 请求（查全部）
{"tool": "query_boss_history", "arguments": {}}
// 响应
"## Mantis\n- 习性: 第一阶段飞扑，第二阶段旋风刀\n\n## Centipede\n- 习性: 直线冲撞\n"
```

### clean_cache
```jsonc
// 请求
{"tool": "clean_cache", "arguments": {"target": "predict"}}
// 响应
"已清理预判历史: /tmp/predict_history.json"
```

### switch_tactic
```jsonc
// 请求
{"tool": "switch_tactic", "arguments": {"tactic_file": "florr_combat.md"}}
// 响应
"已切换当前战术为: florr_combat.md"
```

### ugf_guide
```jsonc
// 请求（全部）
{"tool": "ugf_guide", "arguments": {"section": "all"}}
// 请求（仅工具清单）
{"tool": "ugf_guide", "arguments": {"section": "tools"}}
```
