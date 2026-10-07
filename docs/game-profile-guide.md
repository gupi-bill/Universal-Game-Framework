# 新游戏接入指南（30 分钟）

> 目标：不看源码，给一款新游戏写出可跑档案。全程离线（mock + dry-run），不碰键鼠。
> 对照样例：`game_profiles/demo_arcade.yaml`、`game_profiles/space_invaders.yaml`。

## 第 1 步：复制模板（2 分钟）

```bash
cp game_profiles/_template.yaml game_profiles/my_game.yaml
```

模板里每个字段都带类型、安全区间和缺失后果注释。把所有 `__UGF_XXX__` 占位符换成真实值。

## 第 2 步：填五个必填块（15 分钟）

### 2.1 元信息

```yaml
game:
  name: my_game        # 必须与文件名一致，只允许 [\w.-]
  description: "我的小游戏"
```

### 2.2 稀有度档位与威胁金字塔

按你游戏里"什么东西最危险"分四档（名字随意，档位语义固定）：

```yaml
predictor:
  rarity_highest_boss: ["Mothership"]     # 出现即避战
  rarity_boss: ["BigZap"]
  rarity_elite: ["Missile"]
  rarity_normal: ["Asteroid", "UFO"]
  threat:                                  # 7 键必填，逐级递减
    highest_boss: 1000
    boss: 400
    elite: 120
    normal: 15
    player_enemy: 150
    player_ally: 0
    unknown: 5
```

### 2.3 套装（动作档位）

`sets` 是你游戏里的"真实档位名"（按键顺序 = 数字键 1~9）。若名字与决策语义
（combat/tank/retreat/chase/team）不同名，必须给 `set_map` 翻译：

```yaml
combat:
  chase_min_category: elite
  sets: [fire, shield, evasive]
  set_map:
    combat: fire
    tank: shield
    retreat: evasive
    chase: fire
    team: shield
  default_set: fire
  tactics:
    - "护盾能量低于三成时切闪避档拉开距离，不要恋战"
    - "母舰出现时优先躲避小怪火力，等其转移后再回场"
```

战术条目**带"适用条件"才会真正影响决策**（宁缺毋滥，条件写法参考模板注释）。

### 2.4 mock 感知数据（离线跑通的关键）

```yaml
perception:
  mock:
    drift: true            # 实体按 vx/vy 移动，预判才有东西可算
    afk_popup: false
    player:
      alive: true
      hp: 3
      max_hp: 3
      x: 480
      y: 560
      power_score: 60
      petal_set: fire      # 必须 ∈ combat.sets
    entities:
      - {raw_id: asteroid, rarity: Asteroid, x: 200, y: 100, vx: 40, vy: 60}
      - {raw_id: bigzap,   rarity: BigZap,   x: 700, y: 200, vx: -30, vy: 20}
    teammates: []          # 单机游戏必须显式写 []，否则会串到默认队友
```

`rarity` 必须属于 2.2 声明过的档位——写错离线链路直接跑不通，这是故意的。

### 2.5 端口（可选）

```yaml
server:
  perception_port: 5031   # 每款游戏一个端口，多游戏并行不打架
```

## 第 3 步：校验（1 分钟）

```bash
python agent.py profile-check my_game --strict
```

ERROR 必须清零；WARN 建议处理。校验规则与模板注释一一对应（ROADMAP #10）。

## 第 4 步：离线跑通（2 分钟）

```bash
AGENT_GAME=my_game python agent.py run --dry-run --rounds 30
```

看日志确认：感知出帧 → 预判有实体 → 评估有决策 → 动作被记录（dry-run 不碰键鼠）。

```bash
AGENT_GAME=my_game python agent.py kb search 战术   # 种子知识已自动生成
AGENT_GAME=my_game python agent.py logs --events --tail 10   # 结构化事件流
```

## 第 5 步：接真实感知（可选，进阶）

三选一（详见 [architecture.md](architecture.md) 感知段与 config.yaml 注释）：

| 后端 | 适用 | 依赖 |
|---|---|---|
| `http` | 已有外部检测服务（如 YOLO server） | requests |
| `local` | 本机直接跑 YOLOv8 导出的 .onnx | mss numpy onnxruntime opencv |
| `template` | 画面元素固定、模板匹配就够的小游戏 | mss numpy opencv |

动作端接真机：`pip install pyautogui`，去掉 `--dry-run`，先在**本地/自建/已授权**环境试。

## 常见错误速查

| 现象 | 原因 | 处理 |
|---|---|---|
| profile-check 报 name 不一致 | `game.name` ≠ 文件名 | 改成一致 |
| 离线跑没有实体 | mock.entities 的 rarity 不在声明档位 | 对齐 2.2 的档位表 |
| 换套总是"未知套装" | sets 与决策语义不同名且没写 set_map | 补 set_map |
| 战术不影响决策 | 战术条目没写"适用条件" | 按模板条件句式补 |
| 切游戏后队友数据串味 | mock.teammates 未显式声明 | 单机写 `[]` |
