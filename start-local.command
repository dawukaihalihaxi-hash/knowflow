#!/bin/zsh
set -e
cd "$(dirname "$0")"
export APP_CONFIG="${APP_CONFIG:-config/rag.railway.json}"
export PORT="${PORT:-8085}"
if [[ -x .venv/bin/python3 ]]; then
  .venv/bin/python3 app.py
else
  python3 app.py
fi
