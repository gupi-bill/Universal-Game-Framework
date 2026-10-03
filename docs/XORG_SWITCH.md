# Xorg 切换指南

## 为什么切换

在 GNOME on Wayland 环境中，Xwayland 以 `-rootless` 模式运行，X 根窗口不是真实屏幕。
这导致所有 X11 抓屏工具失败：`import -window root` → BadMatch，`xwd -root` → BadMatch，
`mss` → XProtoError opcode 73，`gnome-screenshot` → 唯一可用，但固定 ~1573ms。

切换到 Xorg 后：

| 指标 | Wayland（原） | Xorg（切后） |
|------|--------------|-------------|
| `XDG_SESSION_TYPE` | wayland | x11 |
| `import -window root` | BadMatch 失败 | 1443 ms |
| `xwd -root` | BadMatch 失败 | 201 ms |
| `mss` | XProtoError | **15 ms**（min 10） |
| `_take_screenshot` | 1573 ms | **219 ms** |

## 切换步骤

```bash
# 1. 提权
sudo -v

# 2. 运行切换脚本
sudo bash ~/xorg-switch/switch_to_xorg.sh --safe

# 3. 立即重启
sudo reboot
```

## 切换后验证

```bash
echo $XDG_SESSION_TYPE                # 期望 x11
bash ~/xorg-switch/verify_xorg_capture.sh
```

`--safe` 模式会自动备份 `/etc/gdm3/daemon.conf` 并临时关闭自动登录，确保能在登录界面手动选回 Wayland。

## 回滚

```bash
# 任选一种
sudo bash ~/xorg-switch/switch_to_xorg.sh --rollback
sudo bash ~/xorg-switch/rollback_xorg.sh
```

## 关键配置

`--safe` 会做两件事：

1. `WaylandEnable=false`（在 `[daemon]` 段）
2. `AutomaticLoginEnable=False`（临时关自动登录，保留会话选择器）

验证通过后恢复自动登录：

```bash
sudo bash ~/xorg-switch/switch_to_xorg.sh --restore-autologin
sudo reboot
```

## 已知问题与 workaround

- **`import -window root` 仍需后台运行 X server**：在纯 Wayland 环境下无法工作，必须切 Xorg。
- **mss 速度不如 x11grab**：mss 是纯 Python 库，抓单帧 ~15ms，但与 gnome-screenshot 1573ms 相比已快 100 倍。
- **`--safe` 模式的副作用**：临时关自动登录后需要手动输密码才能登录，验证完需 `--restore-autologin`。
