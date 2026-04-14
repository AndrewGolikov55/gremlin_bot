from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from app.bot.typing_indicator import TYPING_REFRESH_SECONDS, keep_typing  # noqa: F401


@pytest.mark.asyncio
async def test_pulse_fires_at_least_once_in_short_block() -> None:
    """keep_typing sends chat_action immediately on entry."""
    bot = AsyncMock()
    bot.send_chat_action = AsyncMock()

    async with keep_typing(bot, chat_id=-100):
        await asyncio.sleep(0)  # yield to let the pulse task run

    assert bot.send_chat_action.await_count >= 1
    call = bot.send_chat_action.await_args_list[0]
    assert call.args == (-100, "typing") or call.kwargs.get("action") == "typing"


@pytest.mark.asyncio
async def test_pulse_refreshes_during_long_block(monkeypatch: pytest.MonkeyPatch) -> None:
    """Inside a block longer than TYPING_REFRESH_SECONDS, action is sent multiple times."""
    # Shorten the refresh interval for the test to keep it fast.
    monkeypatch.setattr("app.bot.typing_indicator.TYPING_REFRESH_SECONDS", 0.02)
    bot = AsyncMock()
    bot.send_chat_action = AsyncMock()

    async with keep_typing(bot, chat_id=-100):
        await asyncio.sleep(0.1)  # ~5 refresh intervals

    assert bot.send_chat_action.await_count >= 3


@pytest.mark.asyncio
async def test_pulse_stops_after_exit() -> None:
    """After the block exits, no further calls are made."""
    bot = AsyncMock()
    bot.send_chat_action = AsyncMock()

    async with keep_typing(bot, chat_id=-100):
        await asyncio.sleep(0)
    count_at_exit = bot.send_chat_action.await_count
    await asyncio.sleep(0.1)  # give any orphan task a chance
    assert bot.send_chat_action.await_count == count_at_exit


@pytest.mark.asyncio
async def test_exception_in_block_propagates_and_cancels_pulse() -> None:
    """If the block raises, the exception propagates and the pulse task is cancelled cleanly."""
    bot = AsyncMock()
    bot.send_chat_action = AsyncMock()

    with pytest.raises(RuntimeError, match="boom"):
        async with keep_typing(bot, chat_id=-100):
            raise RuntimeError("boom")

    # After exit, no lingering calls
    count_at_exit = bot.send_chat_action.await_count
    await asyncio.sleep(0.05)
    assert bot.send_chat_action.await_count == count_at_exit


@pytest.mark.asyncio
async def test_send_chat_action_failure_is_swallowed() -> None:
    """If send_chat_action raises, keep_typing does not propagate the error."""
    bot = AsyncMock()
    bot.send_chat_action = AsyncMock(side_effect=RuntimeError("network"))

    # Should complete without raising
    async with keep_typing(bot, chat_id=-100):
        await asyncio.sleep(0)

    assert bot.send_chat_action.await_count >= 1
