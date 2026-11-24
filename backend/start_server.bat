@echo off
chcp 65001 >nul
title 外贸 RAG 后端 (127.0.0.1:8000)
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo [错误] 没找到 .venv，先在 backend 目录建虚拟环境并装依赖。
  pause
  exit /b 1
)

echo 后端启动中…… 浏览器打开 http://127.0.0.1:8000/app/
echo 关掉这个窗口 = 停止服务。日志同步写入 data\uvicorn_dev.log
echo.

.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000 > data\uvicorn_dev.log 2>&1
echo.
echo 服务已退出，最近日志：
type data\uvicorn_dev.log
pause
