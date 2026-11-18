@echo off
chcp 65001 >nul
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" (
  set "PY=.venv\Scripts\python.exe"
) else (
  set "PY=python"
)

echo 正在启动 douyin2weibo 可视化控制台，稍候会自动打开浏览器...
"%PY%" -m src.ui_server
pause
