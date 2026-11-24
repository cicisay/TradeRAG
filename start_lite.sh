#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/backend"

if [ ! -d .venv ]; then
  echo "[lite] first run: creating virtualenv and installing dependencies..."
  python3 -m venv .venv
  .venv/bin/python -m pip install -U pip
  .venv/bin/python -m pip install -r requirements.txt
fi

export VECTOR_STORE=memory
export DATABASE_URL="sqlite:///./data/app.db"
export INGEST_MODE=auto

if [ "${1:-}" = "fast" ]; then
  export EMBED_PROVIDER=hash
  export RERANK_PROVIDER=off
  echo "[lite-fast] hash embedding + no rerank, BM25-only retrieval"
else
  echo "[lite] warmup will auto-rebuild memory vectors from SQLite with real bge-m3"
fi

echo "[lite] starting server: http://127.0.0.1:18081/app/"
exec .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 18081
