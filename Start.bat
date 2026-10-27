@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 漫程 Wander

echo.
echo   漫程 Wander
echo.

where python >nul 2>nul
if errorlevel 1 (
  echo 未找到 Python。请先安装：https://www.python.org/downloads/
  echo 安装时记得勾选 "Add Python to PATH"
  pause
  exit /b 1
)

if not exist .venv (
  echo 首次运行，正在准备环境（约 2-5 分钟，只需一次）...
  python -m venv .venv
  .venv\Scripts\pip install --quiet --upgrade pip
  .venv\Scripts\pip install -r requirements.txt
  if errorlevel 1 (
    echo 依赖安装失败，请检查网络后重试
    pause
    exit /b 1
  )
  echo 环境就绪。
)

echo 正在启动... 浏览器会自动打开 http://localhost:8765
echo （关闭这个窗口即可停止服务）
echo.

start "" http://localhost:8765
.venv\Scripts\python run.py
pause
