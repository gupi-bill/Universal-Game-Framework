# 真机像素链路打通（2026-10-01）

> 环境：Linux + Xwayland（DISPLAY `:1`），1366×768，无 YOLO 权重、无 `.env` 密钥
> 交付：`perception_server.py` 截图探测重写、证据截图、本文档

## 1. 结论

**「真实截图 → 检测契约 → 归一化」这条链路在真机上是通的。**
缺的是 YOLO 权重，不是代码。

此前 devplan 长期把「真实截图」列为「未验证（受本机环境限制）」，
本轮把它从那一栏挪了出来。

## 2. 修了两个真 bug（都是真机才暴露的）

### D6 · `_screenshot_tool()` 只探 PATH 存在性

旧实现按固定顺序 `scrot → import → gnome-screenshot` 取第一个
`shutil.which` 命中的。

本机实测：`import`（ImageMagick）**存在但已损坏**（退出码非 0、不产文件），
而 `gnome-screenshot` 完全可用。于是探测阶段必然选中坏的那个，
每帧截图都失败 —— 而好的那个被跳过了。

改成：**挨个真试一遍**，用第一个能产出非空文件的。
新增 `maim` 作为 Wayland 原生选项。

### D7 · 不信任继承来的 `DISPLAY`

Wayland 会话下 XWayland 通常在 `:1`，`:0` 反而可能没有授权
（本机 `:0` 报 `Authorization required`，`:1` 正常）。

改成：`_displays_to_try()` 先试继承值，再试 `:1`、`:0`。

## 3. 实测证据

```
探测到的截图工具: 'gnome-screenshot'   （用时 1.62s）
继承的 DISPLAY  : （未设置）
_take_screenshot → True
产出: 123521 bytes
尺寸: 1366×768   非黑像素 1048065   ← 真实画面，不是黑屏
```

「继承的 DISPLAY 为空」是关键 —— 说明探测逻辑自己找到了可用显示，
不是靠我手工设 `DISPLAY=:1` 蒙对的。

截图存于 `devplan/REAL_SCREENSHOT_20261001.png`。

## 4. 端到端（真实像素 + stub 检测）

为了把「YOLO 权重缺失」这一个变量隔离掉，临时放了一个 stub 检测脚本，
形状对齐真实 YOLO 输出（`player` / `entities` / `teammates`）。

```
resolve_backend(): mock → http        ← 自动升级
_backend: http
player: hp=88.0 x=683.0 alive=True     ← x 来自真实 1366 宽的一半
entities: 3 个（swarmer / drone / boss）
错误字段: 无
stub 读到的图片: 1366×768  120396 bytes
```

`x=683 = 1366 × 0.5` —— **坐标是从真实像素尺寸算出来的**，
这条链路上的每个环节都在处理真数据。

## 5. 现在还缺什么

| 缺什么 | 影响 | 怎么补 |
|--------|------|--------|
| YOLO 权重 | 检测不到真实实体 | 训练或下载针对目标游戏的权重 |
| `florr_powerful_tools` 真入口 | 目前是 stub | `git clone` 那个仓库替换 stub |
| `.env` 密钥 | LLM/VLM 决策走兜底 | 自备 key |

**注意**：stub 脚本已加进 `.gitignore`（`florr_powerful_tools/`）——
它是本地验证工具，不是项目代码。

## 6. 与 P2 的关系

本轮只证明了**感知层的输入端**能拿到真像素。
P2（知识闭环有增益）目前仍限定在 arena 模拟 —— 要在真实游戏上验证，
还需要「真实截图 + 真实 YOLO + 真实游戏窗口」三者齐备。

这一条已从「❓未知」变成「✓ 输入端已通，中段缺权重」。
