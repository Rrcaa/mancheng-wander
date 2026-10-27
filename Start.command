#!/bin/bash
cd "$(dirname "$0")" || exit 1

echo "✈️  漫程 Wander"
echo ""

PY=""
for c in python3 python; do
  if command -v "$c" >/dev/null 2>&1; then PY="$c"; break; fi
done

if [ -z "$PY" ]; then
  echo "未找到 Python。请先安装：https://www.python.org/downloads/"
  read -n 1 -s -r -p "按任意键退出…"
  exit 1
fi

if [ ! -d .venv ]; then
  echo "首次运行，正在准备环境（约 2–5 分钟，只需一次）…"
  "$PY" -m venv .venv || { echo "创建虚拟环境失败"; read -n 1 -s -r -p "按任意键退出…"; exit 1; }
  ./.venv/bin/pip install --quiet --upgrade pip
  ./.venv/bin/pip install -r requirements.txt || {
    echo "依赖安装失败，请检查网络后重试"
    read -n 1 -s -r -p "按任意键退出…"
    exit 1
  }
  echo "环境就绪。"
fi

echo "正在启动… 浏览器会自动打开 http://localhost:8765"
echo "（关闭这个窗口即可停止服务）"
echo ""

( sleep 3; open http://localhost:8765 ) &
./.venv/bin/python run.py
