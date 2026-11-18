#!/bin/bash
set -u
cd "$(dirname "$0")" || exit 1

if [ -x ".venv/bin/python" ]; then
  PY=".venv/bin/python"
else
  PY="python3"
fi

echo "========================================="
echo "   douyin2weibo：下载 -> 1080P -> 工作台"
echo "========================================="
"$PY" -m src.main --interactive
EXIT_CODE=$?

echo ""
echo "运行结束（退出码：$EXIT_CODE）。按回车键关闭窗口..."
read -r _
