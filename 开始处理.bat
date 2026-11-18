@echo off
chcp 65001 >nul
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" (
  set "PY=.venv\Scripts\python.exe"
) else (
  set "PY=python"
)

echo =========================================
echo    douyin2weibo：下载 -^> 1080P -^> 工作台
echo =========================================
"%PY%" -m src.main --interactive

echo.
pause
