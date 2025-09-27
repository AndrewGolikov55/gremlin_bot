from __future__ import annotations

from apscheduler.schedulers.asyncio import AsyncIOScheduler


def get_scheduler() -> AsyncIOScheduler:
    return AsyncIOScheduler()

