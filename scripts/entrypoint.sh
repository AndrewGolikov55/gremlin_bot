#!/usr/bin/env bash
set -euo pipefail

./scripts/migrate.sh

exec uvicorn app.main:app --host 0.0.0.0 --port 8080
