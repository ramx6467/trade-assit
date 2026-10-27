#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
python3 -m venv "$ROOT/backend/.venv"
"$ROOT/backend/.venv/bin/pip" install -r "$ROOT/backend/requirements.txt"
cd "$ROOT/frontend"
npm install
echo "Setup complete. Start backend and frontend with scripts/run-backend.sh and scripts/run-frontend.sh."
