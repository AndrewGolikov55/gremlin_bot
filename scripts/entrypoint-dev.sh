#!/usr/bin/env bash
set -euo pipefail

export PYTHONPATH="${PYTHONPATH:-/app}"

./scripts/migrate.sh

PORT="${PORT:-8080}"

exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT}" --reload --reload-dir app --reload-dir migrations --reload-dir scripts
