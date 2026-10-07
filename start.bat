@echo off
rem Universal-Game-Framework 一键启动（Windows）
rem 用法: start.bat run --dry-run --rounds 20 / start.bat selftest
chcp 65001 >nul
cd /d "%~dp0"
where python >nul 2>nul || (echo [错误] 未找到 python，请先安装 Python 3.10+ && exit /b 1)
if "%*"=="" (python agent.py run --dry-run --rounds 20) else (python agent.py %*)
pause
