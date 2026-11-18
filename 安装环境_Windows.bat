@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo =========================================
echo    douyin2weibo Windows 首次环境初始化
echo =========================================

if not exist "config.yaml" (
  copy "config.example.yaml" "config.yaml" >nul
  echo [已生成] config.yaml（请记得把抖音和微博 cookie 放到 cookies\ 目录）
)
if not exist "待发.txt" (
  copy "待发.example.txt" "待发.txt" >nul
  echo [已生成] 待发.txt
)
if not exist "cookies" mkdir "cookies"

python --version >nul 2>&1
if errorlevel 1 (
  echo [错误] 未检测到 Python，请先安装 Python 3.12（安装时勾选 Add python.exe to PATH）
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo 正在创建 .venv 虚拟环境...
  python -m venv .venv
)

echo 正在安装 Python 依赖...
.venv\Scripts\python.exe -m pip install -U pip
.venv\Scripts\python.exe -m pip install -r requirements.txt

echo 正在安装 Patchright Chromium 浏览器内核...
.venv\Scripts\patchright.exe install chromium

echo.
echo 初始化完成！
echo 下一步：
echo   1. 确保电脑已安装 ffmpeg（或把 ffmpeg.exe 放到系统 PATH）
echo   2. 把抖音/微博 cookie json 文件放进 cookies\ 目录
echo   3. 双击「启动控制台.bat」即可开始使用！
pause
