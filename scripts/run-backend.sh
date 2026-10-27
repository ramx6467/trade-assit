#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT/backend"
"$ROOT/backend/.venv/bin/alembic" -c alembic.ini upgrade head
exec "$ROOT/backend/.venv/bin/uvicorn" app.main:app --reload --reload-dir app --reload-dir migrations --host 127.0.0.1 --port 8000
