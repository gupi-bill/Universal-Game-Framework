# auto_train.py —— 传游戏名，自动搜资料 + 备数据 + 训练

日期：2026-10-03

## 用法

```bash
python auto_train.py "florr.io"              # 全流程
python auto_train.py "florr.io" --search-only # 只搜资料
python auto_train.py "Terraria" --epochs 50
```

## 设计原则：宁可停下来报错，也不产出假数据

之前踩过的坑：合成数据能训出模型，mAP 看着不低，但和真实画面差得远 ——
**看着能用、实际不能用，比直接报错更糟**。所以 `resolve_data()` 找不到
可用数据源时直接 `sys.exit(2)`，不降级、不瞎编。

## 三种数据来源（自动判定）

| 优先级 | 来源 | 说明 |
|---|---|---|
| a | 本地已有数据集 | `datasets/<game>/` 下有图就直接用 |
| b | 游戏有公开素材 | 下载素材 → 合成标注数据（目前只登记了 florr.io） |
| c | 都没有 | **停下报告缺什么**，并给出三条可行路径 |

## 实测结果（florr.io）

```
[1/4] 搜索资料: florr.io
  ✓ https://florr.io/static/i18n/en_US/mobs.txt → 80 个实体
[2/4] 准备数据
  ✓ 下载 78MB（github 连超时，raw.githubusercontent 成功）
  ✓ 合成器: upload_package/~/generate_data.py
  ✓ 127 训练图 + 30 验证图
[3/4] 训练 → 在 AVX 检查处终止（本机硬件限制，换机器即可跑）
```

搜索能从官方静态文件直接抓到真实实体清单（`mantis` / `termite_overmind` /
`titan` …共 80 个），这部分**对所有游戏都通用**，不只 florr。

## 踩坑记录（都是这个仓库自己的问题）

1. **GitHub 大文件链路不稳** —— `github.com/raw` 反复 ConnectTimeout。
   加了三个源轮换：`github.com/raw` → `raw.githubusercontent.com` →
   `cdn.jsdelivr.net`，每个重试 2 次。

2. **合成器目录名带 `~`** —— `upload_package/~/generate_data.py`。
   所以改成扫描目录找脚本，不写死路径。

3. **类名和参数名改过版** —— 仓库里同时存在
   `SyntheticDataGenerator(samples_per_bg=...)` 和
   `HighQualityDataGenerator(total_samples=...)`。
   现在按 `inspect.signature` 自适应，不写死参数名。

4. **Wikipedia 超时会覆盖更好的状态** —— 维基挂了不该让整次搜索显示
   `error`。现在记到 `_wikipedia_error`，只在 GitHub 也无果时才标 `not_found`。

## 本机硬件限制

`Intel Pentium J2900` 缺 `avx, avx2, fma, bmi1, bmi2, lzcnt`，
PyTorch/Ultralytics 在此会崩（polars 先给出警告，torch 随后静默退出）。
所以**本机只能跑到数据准备为止**，训练需换机器。

换到有 AVX 的机器后直接同一条命令即可，脚本会自动检测 GPU 并使用。

## 局限（诚实说明）

- `KNOWN_SOURCES` 目前只登记了 florr.io。别的游戏走 c 分支会停下报错，
  这是**有意的** —— 等真的接入了新素材源再往表里加。
- 合成数据（贴图到背景图）与真实游戏画面仍有差距。训出来的权重在
  合成集上指标好看，**不等于**在真实画面上可用。这一点必须真机验证。
