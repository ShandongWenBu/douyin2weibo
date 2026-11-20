@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo =========================================
echo    douyin2weibo 从 Git 仓库更新代码
echo =========================================
git --version >nul 2>&1
if errorlevel 1 (
  echo [错误] 未检测到 Git，请先安装 Git for Windows：https://git-scm.com/download/win
  pause
  exit /b 1
)

if exist ".venv\Scripts\python.exe" (
  .venv\Scripts\python.exe -m src.updater
) else (
  git pull --ff-only
)

if errorlevel 1 (
  echo.
  echo [提示] 拉取失败，请检查网络或确认本地是否有未提交改动。
  pause
  exit /b 1
)

echo.
echo 更新完成！现在可以双击「启动控制台.bat」使用了。
pause
