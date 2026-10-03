# MCP 调用示例（给接入方照抄）

> 三种标准套路：**摸清局面 / 打一局 / 学一轮**。
> 所有示例在 dry-run（`UGF_DRY_RUN=1`）下都能跑通，不会真的动键鼠。

## 0. 第一次接入

```jsonc
// 先问它自己会什么
{"tool": "ugf_guide", "arguments": {"section": "all"}}
// 只想看工具清单
{"tool": "ugf_guide", "arguments": {"section": "tools"}}
```

---

## 1) 摸清局面

```jsonc
{"tool": "perceive_game"}                       // 看一帧（自身 HP + 实体列表）
{"tool": "perceive_game"}                       // 连看 3 帧以上，预判才有效
{"tool": "perceive_game"}
{"tool": "predict_all_entities"}                // 未来 1.2s 位置 + 威胁排序
{"tool": "kb_search", "arguments": {"keyword": "当前最强敌人的名字", "game_name": "florr"}}
{"tool": "query_boss_history", "arguments": {"boss_name": "BOSS 名"}}
```

读法：`confidence < 0.65` 时 `prediction_trusted=false`，预判坐标是 `null`，**别信**；
历史帧不足会返回 `{"status":"insufficient_data"}` → 多调几次 `perceive_game`。

---

## 2) 打一局

```jsonc
{"tool": "perceive_game"}
{"tool": "predict_all_entities"}
// 自己评估：打得过就 attack，打不过就 move 到远离最高 threat_score 实体的方向
{"tool": "game_action", "arguments": {"action_type": "attack"}}
{"tool": "game_action", "arguments": {"action_type": "move", "x": 640, "y": 360}}
{"tool": "switch_set",  "arguments": {"set_name": "combat"}}     // 换阵型
{"tool": "handle_afk"}                                            // 出现 AFK 弹窗时
// 收尾
{"tool": "kb_append", "arguments": {"filename": "florr_runs.md", "game_name": "florr",
  "markdown_content": "## 2026-09-22\n- 结论：...\n- 失误：...\n"}}
```

注意：`game_action` 的 `move` **必须**给 `x` 和 `y`；切套装用 `switch_set`，不要用 `game_action`。

---

## 3) 学一轮

```jsonc
{"tool": "kb_list",   "arguments": {"game_name": "florr"}}        // 现在有哪些资料
{"tool": "kb_search", "arguments": {"keyword": "走位"}}             // 找缺口
{"tool": "kb_write",  "arguments": {"filename": "tactic_kiting.md", "game_name": "florr",
  "markdown_content": "# 风筝战术\n1. ...\n2. ...\n"}}
{"tool": "switch_tactic", "arguments": {"tactic_file": "tactic_kiting.md"}}  // 设为当前战术
{"tool": "kb_export"}                                              // 定期备份
```

---

## 换局 / 长时间挂机的维护动作

```jsonc
{"tool": "reset_predictor"}                       // 换局或重生后必做
{"tool": "clean_cache", "arguments": {"target": "frames"}}   // 清临时帧，省磁盘
{"tool": "clean_cache", "arguments": {"target": "all"}}
{"tool": "kb_import", "arguments": {"backup_path": "/绝对路径/backup.tar.gz"}}  // 恢复资料
```

---

## 常见报错对照

| 返回 | 含义 | 怎么做 |
|---|---|---|
| `未知动作类型: xxx，可选 ...` | `action_type` 拼错 | 用返回值里列出的可选值 |
| `move 动作必须提供 x 和 y 坐标` | move 缺坐标 | 补上 x / y |
| `未知套装: xxx，可选 ...` | 套装名不在当前游戏档案里 | 用返回值列出的名字 |
| `{"status":"insufficient_data"}` | 历史帧 < 3 | 多调几次 `perceive_game` |
| `错误: 非法的知识库路径` | 文件名/游戏名含 `..` 或路径穿越 | 去掉路径分隔符与 `..` |
| 结果里带 `_fallback` | 感知服务没起，用了合成帧 | 正常现象；上真机前再起感知服务 |

---

## 附录：环境差异（dry-run vs 真机）

| 环境变量 | 效果 |
|---|---|
| `UGF_DRY_RUN=1` | 所有动作只打日志，不动键鼠；感知走 mock/合成帧，不依赖感知服务 |
| `UGF_PERCEPTION_BACKEND=mock` | 强制用合成感知，完全离线 |
| `UGF_PERCEPTION_BACKEND=http` | 走真实截图+YOLO；感知服务必须独立启动 |
| `UGF_YOLO_WEIGHT` | 指定 YOLO 权重文件路径 |

### 模型缓存

第一次调用 `perceive_game` 时，YOLO 模型会被加载到内存（约 19 秒）。
之后的调用复用同一模型，每模型只加载一次。

### 超时链

```
client timeout (24s)          |  perception_server
  |                           |    yolo_timeout (8s)
  +--- HTTP request --------> +--- 子进程 detect.py
                                      |-- 常驻模型 predict (~700ms)
```

模型未加载完时第一帧会慢，客户端无需适配，超时等待即可。
