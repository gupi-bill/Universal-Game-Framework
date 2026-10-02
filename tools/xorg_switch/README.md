# 重启后照着做

切换到 Xorg 会话的脚本都在这个目录（`~/xorg-switch/`）。
`/tmp/opencode/` 里的副本已被重启清掉，以这里为准。

---

## 1. 登录时确认进了 Xorg

GDM 登录界面正常输密码即可（默认已是 Xorg）。
**万一黑屏或异常**：点右下角齿轮，手动选 **GNOME on Xorg**。

## 2. 跑验证脚本

```bash
bash ~/xorg-switch/verify_xorg_capture.sh
```

看两个关键行：

- `XDG_SESSION_TYPE = x11` → 切换成功（是 `wayland` 就是没生效）
- `端到端/帧: 约 xxx ms` → 对比基线

## 3. 基线对照

| 指标 | 切换前（Wayland） | 期望（Xorg） |
|---|---|---|
| `XDG_SESSION_TYPE` | `wayland` | `x11` |
| `import -window root` | BadMatch 失败 | ~50 ms |
| `xwd -root` | BadMatch 失败 | 可用 |
| `mss` | XProtoError 崩溃 | ~30 ms |
| 截图 | 1573 ms | ~50 ms |
| 端到端/帧 | ~2400 ms | < 800 ms |

## 4. 要回滚的话

任选一种：

```bash
# 方式 A：登录界面齿轮选 "GNOME on Wayland"（不用命令行，最快）

# 方式 B：恢复备份
sudo bash ~/xorg-switch/rollback_xorg.sh

# 方式 C：黑屏进不去时，Ctrl+Alt+F2 切 tty，用 root 登录后
sudo cp -a /etc/gdm3/custom.conf.bak-* /etc/gdm3/custom.conf && sudo reboot
```

## 5. 验证通过后还没做的事

- 按实测结果调整 `perception_server._SCREENSHOT_CANDIDATES` 的优先级
  （`import` / `mss` 比 `gnome-screenshot` 快一个量级，后者应降为兜底）
- 这个改动还没进 UGF 主代码，等验证数据出来再动

## 相关文档

`~/文档/Default Project/Universal-Game-Framework/devplan/P2_REAL_YOLO_20261001.md`
的「追加」和「追加二」两节记录了完整的诊断数据和踩坑过程。
