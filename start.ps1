# Universal-Game-Framework 一键启动（Windows PowerShell）
# 用法: .\start.ps1 run --dry-run --rounds 20   或   .\start.ps1 selftest
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
Set-Location $PSScriptRoot
if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    Write-Error "未找到 python，请先安装 Python 3.10+"
    exit 1
}
if ($args.Count -eq 0) { python agent.py run --dry-run --rounds 20 }
else { python agent.py @args }
