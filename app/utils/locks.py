from __future__ import annotations

import asyncio


def get_chat_lock(chat_id: int, locks: dict[int, asyncio.Lock]) -> asyncio.Lock:
    """Return the per-chat asyncio.Lock from `locks`, creating it lazily."""
    lock = locks.get(chat_id)
    if lock is None:
        lock = asyncio.Lock()
        locks[chat_id] = lock
    return lock
