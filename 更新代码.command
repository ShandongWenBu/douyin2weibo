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

git pull --ff-only
EXIT_CODE=$?

if [ $EXIT_CODE -eq 0 ] && [ -x ".venv/bin/python" ]; then
  if command -v uv >/dev/null 2>&1; then
    echo "正在检查 Python 依赖..."
    uv pip install --python .venv/bin/python -r requirements.txt
  fi
fi

echo ""
echo "运行结束（退出码：$EXIT_CODE）。按回车键关闭窗口..."
read -r _
