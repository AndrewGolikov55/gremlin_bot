from __future__ import annotations

import unittest.mock as um
from datetime import date, datetime
from unittest.mock import AsyncMock, create_autospec
from zoneinfo import ZoneInfo

import pytest
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramBadRequest

from app.models import RouletteWinner
from app.services.app_config import AppConfigService
from app.services.llm.client import LLMError
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


@pytest.mark.asyncio
async def test_render_winner_calls_llm_with_top(sessionmaker):
    from app.services.roulette import StatsEntry

    captured: dict = {}

    async def fake_generate(messages, **kwargs):
        captured["messages"] = messages
        captured["kwargs"] = kwargs
        return "🏆 Король Мудаков месяца — Андрей! Ёбана."

    bot = AsyncMock()
    svc = _make_service(sessionmaker, bot)
    svc.app_config.get_all = AsyncMock(return_value={"llm_provider": "openrouter"})

    with um.patch("app.services.monthly_champion.llm_generate", fake_generate):
        text = await svc._render_winner(
            top=[
                StatsEntry(user_id=1, username="Андрей", wins=7),
                StatsEntry(user_id=2, username="Семён", wins=4),
            ],
            champion_name="Андрей",
            daily_title="Мудак дня",
            month_label="апрель 2026",
        )

    assert "Король" in text or "Андрей" in text
    assert any("Мудак дня" in m["content"] for m in captured["messages"])
    assert any("Андрей" in m["content"] for m in captured["messages"])


@pytest.mark.asyncio
async def test_render_winner_falls_back_when_llm_fails(sessionmaker):
    from app.services.roulette import StatsEntry

    async def fake_generate(messages, **kwargs):
        raise LLMError("provider down")

    bot = AsyncMock()
    svc = _make_service(sessionmaker, bot)
    svc.app_config.get_all = AsyncMock(return_value={})

    with um.patch("app.services.monthly_champion.llm_generate", fake_generate):
        text = await svc._render_winner(
            top=[StatsEntry(user_id=1, username="Андрей", wins=7)],
            champion_name="Андрей",
            daily_title="Мудак дня",
            month_label="апрель 2026",
        )

    assert "Андрей" in text
    assert "Мудак" in text


@pytest.mark.asyncio
async def test_render_runoff_winner(sessionmaker):
    async def fake_generate(messages, **kwargs):
        return "Победил Андрей! Король Мудаков месяца, ёбана."

    bot = AsyncMock()
    svc = _make_service(sessionmaker, bot)
    svc.app_config.get_all = AsyncMock(return_value={})

    with um.patch("app.services.monthly_champion.llm_generate", fake_generate):
        text = await svc._render_runoff_winner(
            tied_names=["Андрей", "Семён"],
            winner_name="Андрей",
            daily_title="Мудак дня",
        )
    assert "Андрей" in text


@pytest.mark.asyncio
async def test_render_runoff_falls_back_to_text_when_llm_fails(sessionmaker):
    async def fake_generate(messages, **kwargs):
        raise LLMError("down")

    bot = AsyncMock()
    svc = _make_service(sessionmaker, bot)
    svc.app_config.get_all = AsyncMock(return_value={})

    with um.patch("app.services.monthly_champion.llm_generate", fake_generate):
        text = await svc._render_runoff_winner(
            tied_names=["Андрей", "Семён"],
            winner_name="Андрей",
            daily_title="Мудак дня",
        )
    assert "Андрей" in text
    assert "Мудак" in text


@pytest.mark.asyncio
async def test_render_empty_month(sessionmaker):
    async def fake_generate(messages, **kwargs):
        return "В этом месяце короля не нашлось."

    bot = AsyncMock()
    svc = _make_service(sessionmaker, bot)
    svc.app_config.get_all = AsyncMock(return_value={})

    with um.patch("app.services.monthly_champion.llm_generate", fake_generate):
        text = await svc._render_empty(daily_title="Мудак дня", month_label="апрель 2026")
    assert text


@pytest.mark.asyncio
async def test_render_empty_falls_back(sessionmaker):
    async def fake_generate(messages, **kwargs):
        raise LLMError("down")

    bot = AsyncMock()
    svc = _make_service(sessionmaker, bot)
    svc.app_config.get_all = AsyncMock(return_value={})

    with um.patch("app.services.monthly_champion.llm_generate", fake_generate):
        text = await svc._render_empty(daily_title="Мудак дня", month_label="апрель 2026")
    assert "Мудак" in text
