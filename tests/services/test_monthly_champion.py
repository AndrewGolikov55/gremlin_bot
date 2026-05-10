from __future__ import annotations

from datetime import date, datetime
from unittest.mock import AsyncMock, create_autospec
from zoneinfo import ZoneInfo

import pytest
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramBadRequest

from app.models import RouletteWinner
from app.services.app_config import AppConfigService
from app.services.monthly_champion import MonthlyChampionService, _previous_period  # noqa: F401
from app.services.roulette import RouletteService
from app.services.settings import SettingsService


def test_previous_period_basic():
    # 1 мая 12:00 MSK → period [2026-04-01, 2026-05-01)
    now = datetime(2026, 5, 1, 12, 0, 0, tzinfo=ZoneInfo("Europe/Moscow"))
    start, end_excl = _previous_period(now)
    assert start == date(2026, 4, 1)
    assert end_excl == date(2026, 5, 1)


def test_previous_period_january():
    # 1 января 12:00 MSK → period [2025-12-01, 2026-01-01)
    now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=ZoneInfo("Europe/Moscow"))
    start, end_excl = _previous_period(now)
    assert start == date(2025, 12, 1)
    assert end_excl == date(2026, 1, 1)


def test_previous_period_mid_month():
    # 15 мая 03:00 MSK → period [2026-04-01, 2026-05-01)
    now = datetime(2026, 5, 15, 3, 0, 0, tzinfo=ZoneInfo("Europe/Moscow"))
    start, end_excl = _previous_period(now)
    assert start == date(2026, 4, 1)
    assert end_excl == date(2026, 5, 1)


def test_previous_period_requires_aware_datetime():
    naive = datetime(2026, 5, 1, 12, 0, 0)
    with pytest.raises(ValueError):
        _previous_period(naive)


def _make_member(name: str | None, *, username: str | None = None, status=ChatMemberStatus.MEMBER):
    m = type("M", (), {})()
    m.status = status
    m.user = type("U", (), {})()
    m.user.first_name = name
    m.user.username = username
    return m


def _make_service(sessionmaker, bot):
    return MonthlyChampionService(
        sessionmaker=sessionmaker,
        bot=bot,
        roulette=create_autospec(RouletteService, instance=True),
        settings=create_autospec(SettingsService, instance=True),
        app_config=create_autospec(AppConfigService, instance=True),
    )


@pytest.mark.asyncio
async def test_resolve_display_name_active_member(sessionmaker):
    bot = AsyncMock()
    bot.get_chat_member = AsyncMock(return_value=_make_member("Андрей", username="andrew"))

    svc = _make_service(sessionmaker, bot)
    name = await svc._resolve_display_name(chat_id=42, user_id=100)
    assert name == "Андрей"


@pytest.mark.asyncio
async def test_resolve_display_name_uses_username_when_no_first_name(sessionmaker):
    bot = AsyncMock()
    bot.get_chat_member = AsyncMock(return_value=_make_member(None, username="andrew"))

    svc = _make_service(sessionmaker, bot)
    name = await svc._resolve_display_name(chat_id=42, user_id=100)
    assert name == "andrew"


@pytest.mark.asyncio
async def test_resolve_display_name_left_falls_back_to_winner_snapshot(sessionmaker):
    chat_id = 42
    async with sessionmaker() as session:
        session.add(RouletteWinner(
            chat_id=chat_id, user_id=100, username="andrew_old",
            won_at=date(2026, 4, 5), title="t", title_code="test",
            created_at=datetime(2026, 4, 5, 10, 0, 0),
        ))
        await session.commit()

    bot = AsyncMock()
    bot.get_chat_member = AsyncMock(return_value=_make_member(None, status=ChatMemberStatus.LEFT))

    svc = _make_service(sessionmaker, bot)
    name = await svc._resolve_display_name(chat_id=chat_id, user_id=100)
    assert name == "andrew_old"


@pytest.mark.asyncio
async def test_resolve_display_name_get_chat_member_raises(sessionmaker):
    chat_id = 42
    async with sessionmaker() as session:
        session.add(RouletteWinner(
            chat_id=chat_id, user_id=100, username="snapshot_name",
            won_at=date(2026, 4, 5), title="t", title_code="test",
            created_at=datetime(2026, 4, 5, 10, 0, 0),
        ))
        await session.commit()

    bot = AsyncMock()
    bot.get_chat_member = AsyncMock(
        side_effect=TelegramBadRequest(method=None, message="user not found")  # type: ignore[arg-type]
    )

    svc = _make_service(sessionmaker, bot)
    name = await svc._resolve_display_name(chat_id=chat_id, user_id=100)
    assert name == "snapshot_name"


@pytest.mark.asyncio
async def test_resolve_display_name_no_snapshot_returns_id_string(sessionmaker):
    bot = AsyncMock()
    bot.get_chat_member = AsyncMock(
        side_effect=TelegramBadRequest(method=None, message="not found")  # type: ignore[arg-type]
    )

    svc = _make_service(sessionmaker, bot)
    name = await svc._resolve_display_name(chat_id=42, user_id=100)
    assert name == "id100"
