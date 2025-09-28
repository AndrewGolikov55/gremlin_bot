#!/usr/bin/env bash
set -euo pipefail

echo "[*] Waiting for database"
python - <<'PY'
import asyncio
import os

import asyncpg


async def wait_for_db():
    raw_url = os.getenv("DATABASE_URL", "postgresql+asyncpg://bot:bot@db:5432/botdb")
    # asyncpg expects postgresql://
    url = raw_url.replace("+asyncpg", "")
    attempts = 0
    while True:
        attempts += 1
        try:
            conn = await asyncpg.connect(url)
        except Exception as exc:  # pragma: no cover - best effort log
            if attempts >= 30:
                raise SystemExit(f"Database unavailable after {attempts} attempts: {exc}")
            await asyncio.sleep(1)
        else:
            await conn.close()
            break


asyncio.run(wait_for_db())
PY

echo "[*] Running database migrations"
alembic upgrade head
