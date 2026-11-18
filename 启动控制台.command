#!/bin/bash
set -u
cd "$(dirname "$0")" || exit 1

if [ -x ".venv/bin/python" ]; then
  PY=".venv/bin/python"
else
  PY="python3"
fi

echo "正在启动 douyin2weibo 可视化控制台，稍候会自动打开浏览器..."
"$PY" -m src.ui_server
