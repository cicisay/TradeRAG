@echo off
rem Lite mode: run backend natively WITHOUT Docker Desktop.
rem Uses: SQLite (data/app.db) + in-memory vector store + local bge-m3/reranker models.
rem Chat/LLM still calls MiniMax/DeepSeek over network (needs internet).
rem Usage:
rem   start_lite.bat          normal lite mode (real models, auto reindex, ~1-2 min startup)
rem   start_lite.bat fast     ultra-light mode (hash embedding, no rerank, starts in seconds)

cd /d "%~dp0backend"

where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python not found. Install Python 3.11 or later first.
    pause
    exit /b 1
)

rem --- first run: create the virtualenv and install dependencies ---
if not exist ".venv\Scripts\python.exe" (
    echo [lite] first run: creating virtualenv and installing dependencies...
    echo [lite] this may take several minutes.
    python -m venv .venv
    if errorlevel 1 (
        echo [ERROR] failed to create virtualenv.
        pause
        exit /b 1
    )
    .venv\Scripts\python.exe -m pip install -U pip
    .venv\Scripts\python.exe -m pip install -r requirements.txt
    if errorlevel 1 (
        echo [ERROR] failed to install dependencies.
        pause
        exit /b 1
    )
)

rem --- env overrides (real env vars take priority over .env) ---
set "VECTOR_STORE=memory"
set "DATABASE_URL=sqlite:///./data/app.db"
set "INGEST_MODE=auto"

if /I "%~1"=="fast" (
    set "EMBED_PROVIDER=hash"
    set "RERANK_PROVIDER=off"
    echo [lite-fast] hash embedding + no rerank, BM25-only retrieval, starting in seconds...
) else (
    echo [lite] warmup will auto-rebuild memory vectors from SQLite with real bge-m3, ~1 min...
)

echo.
echo [lite] starting server:  http://127.0.0.1:18081/app/
echo [lite] accounts: demo_sales/demo123456  admin/admin123  demo_kb/demo123456
echo [lite] press Ctrl+C to stop.
echo.
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 18081
