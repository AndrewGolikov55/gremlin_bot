#!/usr/bin/env bash
set -euo pipefail

export PYTHONPATH="${PYTHONPATH:-/app}"

./scripts/migrate.sh

exec uvicorn app.main:app --host 0.0.0.0 --port 8080 --reload --reload-dir app --reload-dir migrations --reload-dir scripts
