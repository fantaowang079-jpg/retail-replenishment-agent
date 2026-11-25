#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")"
[ -f config.local.toml ] || cp config.example.toml config.local.toml
[ -d .venv ] || python3 -m venv .venv
if [ ! -f .venv/ready.flag ]; then
  .venv/bin/python -m pip install -r requirements.txt
  touch .venv/ready.flag
fi
exec .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port "${PORT:-8000}" --workers 1
