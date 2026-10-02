# 切 Xorg 会话 —— 重启后照着做

## 第一次该跑哪条

```bash
sudo bash ~/xorg-switch/switch_to_xorg.sh --safe
sudo reboot
```

**为什么第一次不要直接 reboot**：之前的切换其实没生效（脚本第一步就失败了），
配置从没被改过。这次先跑脚本，看它打印「✓ WaylandEnable=false 已生效」再重启。

## 为什么要 `--safe`

本机 `/etc/gdm3/daemon.conf` 里 `AutomaticLoginEnable=True`（自动登录）。
自动登录**不给会话选择器** —— 所以万一 Xorg 起不来，登录界面没法手动选回 Wayland。

`--safe` 会临时把自动登录关掉，让你能在登录界面：

- 正常情况：输密码直接进（默认已是 Xorg）
- 想手动挑：右下角齿轮 → GNOME on Xorg
- 想当场退出：齿轮选 GNOME on Wayland，不用重启

验证没问题后恢复：

```bash
sudo bash ~/xorg-switch/switch_to_xorg.sh --restore-autologin
sudo reboot
```

## 验证

```bash
echo $XDG_SESSION_TYPE                              # 期望 x11
bash ~/xorg-switch/verify_xorg_capture.sh
```

⚠ **看脚本第 0 段的负载提示**。刚重启时机器负载很高（实测 load 8~9、
可用内存 600MB），此时延迟数据偏大、不可与基线比。等开机 10 分钟以上、
负载降到 1 以下再测一次。

| 指标 | Wayland（当前） | Xorg（目标） |
|---|---|---|
| `XDG_SESSION_TYPE` | `wayland` | `x11` |
| `import -window root` | BadMatch 失败 | ~50 ms |
| `xwd -root` | BadMatch 失败 | 可用 |
| `mss` | XProtoError 崩溃 | ~30 ms |
| 截图 | 1573 ms | ~50 ms |

## 回滚

```bash
sudo bash ~/xorg-switch/switch_to_xorg.sh --rollback   # 注释掉 WaylandEnable
# 或
sudo bash ~/xorg-switch/rollback_xorg.sh
```

## 其它子命令

```bash
sudo bash ~/xorg-switch/switch_to_xorg.sh --check   # 只看当前状态
sudo bash ~/xorg-switch/switch_to_xorg.sh --force   # 只切 Xorg，保留自动登录（无退路）
```

## 记录在哪

`~/文档/Default Project/Universal-Game-Framework/devplan/P2_REAL_YOLO_20261001.md`
的「追加三」及之后各节，含完整踩坑记录。
