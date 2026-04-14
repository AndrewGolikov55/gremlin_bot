"""Chat-action 'typing' indicator that refreshes while a block runs.

Telegram expires typing action after ~5 seconds; this context manager
re-sends every TYPING_REFRESH_SECONDS so 'bot is typing...' stays
visible as long as the wrapped block is in progress.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from aiogram import Bot

logger = logging.getLogger(__name__)

TYPING_REFRESH_SECONDS = 4.0


@asynccontextmanager
async def keep_typing(bot: Bot, chat_id: int) -> AsyncIterator[None]:
    """Keep sending chat_action('typing') to the given chat for the duration of the block.

    A background pulse task is started on entry and cancelled on exit
    (normal or exceptional). Individual send_chat_action failures are
    swallowed - a flaky indicator must not break the real response path.
    """
    stop = asyncio.Event()

    async def pulse() -> None:
        while not stop.is_set():
            try:
                await bot.send_chat_action(chat_id, "typing")
            except Exception:
                logger.debug(
                    "send_chat_action failed chat=%s", chat_id, exc_info=True,
                )
            try:
                await asyncio.wait_for(stop.wait(), timeout=TYPING_REFRESH_SECONDS)
            except asyncio.TimeoutError:
                pass

    task = asyncio.create_task(pulse())
    try:
        yield
    finally:
        stop.set()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
