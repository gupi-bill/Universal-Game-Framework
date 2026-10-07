# 常驻运行指南（Windows / Linux）

> 适用场景：7×24 驻守长跑（配合 `autonomous-grind` 类工作流）。
> 三种 Windows 方案按简单→专业排序；Linux 附 systemd 单元。
> 全部方案都建议：`UGF_HOME` 固定数据目录 + dry-run 先验证 + 日志轮转已内置。

## 方案一：任务计划程序（推荐，零依赖）

开机自启 + 崩溃自动重拉：

```bat
schtasks /Create /TN "UGF-Agent" /TR "cmd /c cd /d C:\ugf && set UGF_HOME=C:\ugf-data&& python agent.py run --hours 8" /SC ONLOGON /RL LIMITED /F
schtasks /Run /TN "UGF-Agent"
```

- `/SC ONLOGON`：登录即启动；改用 `/SC ONSTART` 则开机启动（无需登录，需管理员）
- 崩溃重拉：任务计划程序 GUI → 该任务 → 设置 → 勾选「如果任务失败，按以下频率重新启动」
- 停止：`schtasks /End /TN "UGF-Agent"`；删除：`schtasks /Delete /TN "UGF-Agent" /F`

## 方案二：NSSM 注册为 Windows 服务（专业）

```bat
nssm install UGF-Agent "C:\Python312\python.exe" "C:\ugf\agent.py run"
nssm set UGF-Agent AppDirectory C:\ugf
nssm set UGF-Agent AppEnvironmentExtra UGF_HOME=C:\ugf-data
nssm set UGF-Agent AppStdout C:\ugf-data\service.log
nssm set UGF-Agent AppExit Default Restart
nssm start UGF-Agent
```

卸载：`nssm stop UGF-Agent && nssm remove UGF-Agent confirm`

## 方案三：PowerShell 守护循环（临时）

```powershell
while ($true) {
    python agent.py run --hours 4
    Write-Host "agent 退出（$LASTEXITCODE），10 秒后重启…"; Start-Sleep 10
}
```

配合 `--resume` 可从检查点续跑：`python agent.py run --hours 4 --resume`。

## Linux：systemd 单元

```ini
# /etc/systemd/system/ugf-agent.service
[Unit]
Description=Universal-Game-Framework Agent
After=network.target

[Service]
WorkingDirectory=/opt/ugf
Environment=UGF_HOME=/var/lib/ugf
Environment=UGF_DRY_RUN=1
Environment=UGF_PERCEPTION_BACKEND=mock
ExecStart=/usr/bin/python3 /opt/ugf/agent.py run --resume
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now ugf-agent
journalctl -u ugf-agent -f          # 看日志
```

## 长跑注意事项

- **紧急停止**：真实键鼠模式下，鼠标甩到屏幕左上角即暂停（内置 failsafe）
- **日志**：`run_logs/` 按 `logs.max_size_mb` 自动 gzip 轮转、按 `retention_days` 清理
- **知识库膨胀**：`agent.kb_max_mb` 超限自动归档；可定期 `kb maintain`
- **断点**：`--resume` 依赖 `run_logs/loop_checkpoint.json`（正常收尾会自动清除）
