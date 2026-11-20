#!/bin/bash
set -u
cd "$(dirname "$0")" || exit 1

echo "========================================="
echo "   douyin2weibo 从 Git 仓库更新代码"
echo "========================================="

if ! command -v git >/dev/null 2>&1; then
  echo "[错误] 未检测到 git，请先安装 Xcode Command Line Tools。"
  read -r _
  exit 1
fi

if [ -x ".venv/bin/python" ]; then
  .venv/bin/python -m src.updater
  EXIT_CODE=$?
else
  git pull --ff-only
  EXIT_CODE=$?
fi

echo ""
echo "运行结束（退出码：$EXIT_CODE）。按回车键关闭窗口..."
read -r _
