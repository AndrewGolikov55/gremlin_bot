from __future__ import annotations

from datetime import date, datetime
from unittest.mock import AsyncMock, MagicMock, create_autospec
from zoneinfo import ZoneInfo

import pytest

from app.bot.router_admin import cmd_roulette_force_summary
from app.services.monthly_champion import MonthlyChampionService


@pytest.mark.asyncio
async def test_force_summary_runs_for_current_chat(monkeypatch):
    message = MagicMock()
    message.chat.id = 42
    message.chat.type = "supergroup"
    message.from_user.id = 100
    message.reply = AsyncMock()

    bot = AsyncMock()

    async def fake_admin_check(_bot, _chat_id, _user_id):
        return True

    monkeypatch.setattr("app.bot.router_admin._is_group_admin", fake_admin_check)

    monthly = create_autospec(MonthlyChampionService, instance=True)
    monthly.process_chat = AsyncMock()

    fixed = datetime(2026, 5, 5, 12, 0, 0, tzinfo=ZoneInfo("Europe/Moscow"))

    import datetime as _real
    class _DT:
        @staticmethod
        def now(tz=None):
            return fixed if tz else fixed.replace(tzinfo=None)
        combine = staticmethod(_real.datetime.combine)
        utcnow = staticmethod(_real.datetime.utcnow)

    monkeypatch.setattr("app.bot.router_admin.datetime", _DT)

    await cmd_roulette_force_summary(message, bot=bot, monthly_champion=monthly)

    monthly.process_chat.assert_awaited_once()
    call = monthly.process_chat.await_args
    assert call.kwargs["chat_id"] == 42
    assert call.kwargs["period_start"] == date(2026, 4, 1)
    assert call.kwargs["period_end_excl"] == date(2026, 5, 1)


@pytest.mark.asyncio
async def test_force_summary_rejects_private_chat(monkeypatch):
    message = MagicMock()
    message.chat.type = "private"
    message.reply = AsyncMock()
    bot = AsyncMock()
    monthly = create_autospec(MonthlyChampionService, instance=True)
    monthly.process_chat = AsyncMock()

    await cmd_roulette_force_summary(message, bot=bot, monthly_champion=monthly)

    monthly.process_chat.assert_not_awaited()
    message.reply.assert_awaited()


@pytest.mark.asyncio
async def test_force_summary_rejects_non_admin(monkeypatch):
    message = MagicMock()
    message.chat.id = 42
    message.chat.type = "supergroup"
    message.from_user.id = 100
    message.reply = AsyncMock()

    async def fake_admin_check(_bot, _chat_id, _user_id):
        return False

    monkeypatch.setattr("app.bot.router_admin._is_group_admin", fake_admin_check)

    bot = AsyncMock()
    monthly = create_autospec(MonthlyChampionService, instance=True)
    monthly.process_chat = AsyncMock()

    await cmd_roulette_force_summary(message, bot=bot, monthly_champion=monthly)

    monthly.process_chat.assert_not_awaited()
    message.reply.assert_awaited()
