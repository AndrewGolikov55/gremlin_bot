from __future__ import annotations

from datetime import date, datetime
from unittest.mock import AsyncMock, create_autospec

import pytest
from aiogram.enums import ChatMemberStatus
from sqlalchemy import select

from app.models import (
    Chat,
    ChatMemory,
    MonthlyChampion,
    RouletteScoreAdjustment,
    RouletteWinner,
)
from app.services.app_config import AppConfigService
from app.services.context import ContextService
from app.services.monthly_champion import MonthlyChampionService
from app.services.persona import StylePromptService
from app.services.roulette import RouletteService
from app.services.settings import SettingsService
from app.services.user_memory import UserMemoryService


def _member(name: str, *, username: str = "") -> object:
    m = type("M", (), {})()
    m.status = ChatMemberStatus.MEMBER
    m.user = type("U", (), {})()
    m.user.first_name = name
    m.user.username = username
    return m


@pytest.mark.asyncio
async def test_e2e_announces_winner_with_real_aggregator(sessionmaker):
    chat_id = 42
    period_start = date(2026, 4, 1)
    period_end_excl = date(2026, 5, 1)

    async with sessionmaker() as session:
        session.add(Chat(id=chat_id, title="Test", is_active=True))
        # April: user 100 wins x 5
        for d in [5, 10, 15, 20, 25]:
            session.add(RouletteWinner(
                chat_id=chat_id, user_id=100, username="andrew",
                won_at=date(2026, 4, d), title="Мудак дня", title_code="boss",
                created_at=datetime(2026, 4, d, 10, 0, 0),
            ))
        # April: user 101 wins x 2
        for d in [3, 18]:
            session.add(RouletteWinner(
                chat_id=chat_id, user_id=101, username="semen",
                won_at=date(2026, 4, d), title="Мудак дня", title_code="boss",
                created_at=datetime(2026, 4, d, 10, 0, 0),
            ))
        # May noise — should be filtered out
        session.add(RouletteWinner(
            chat_id=chat_id, user_id=101, username="semen",
            won_at=date(2026, 5, 2), title="Мудак дня", title_code="boss",
            created_at=datetime(2026, 5, 2, 10, 0, 0),
        ))
        # adjustment in April removes 1 point from user 100 → total 4 vs 2
        session.add(RouletteScoreAdjustment(
            chat_id=chat_id, user_id=100, delta=-1, reason="guess",
            created_at=datetime(2026, 4, 12, 10, 0, 0),
        ))
        await session.commit()

    bot = AsyncMock()
    bot.get_chat_member = AsyncMock(return_value=_member("Андрей", username="andrew"))
    bot.send_message = AsyncMock()

    # Real RouletteService — only _aggregate is exercised in this flow
    roulette = RouletteService(
        bot=bot,
        sessionmaker=sessionmaker,
        settings=create_autospec(SettingsService, instance=True),
        app_config=create_autospec(AppConfigService, instance=True),
        context=create_autospec(ContextService, instance=True),
        personas=create_autospec(StylePromptService, instance=True),
        memory=create_autospec(UserMemoryService, instance=True),
    )

    settings = create_autospec(SettingsService, instance=True)
    settings.get_all = AsyncMock(return_value={"is_active": True, "roulette_custom_title": "Мудак дня"})
    app_config = create_autospec(AppConfigService, instance=True)
    app_config.get_all = AsyncMock(return_value={})

    svc = MonthlyChampionService(
        sessionmaker=sessionmaker, bot=bot,
        roulette=roulette, settings=settings, app_config=app_config,
    )

    import unittest.mock as um
    async def fake_gen(messages, **kw):
        return "🏆 Король Мудаков месяца — Андрей! Отжал титул честно."
    with um.patch("app.services.monthly_champion.llm_generate", fake_gen):
        await svc.process_chat(
            chat_id=chat_id, period_start=period_start, period_end_excl=period_end_excl,
        )

    # Exactly one message sent
    assert bot.send_message.await_count == 1
    sent = bot.send_message.call_args
    text_arg = sent.kwargs.get("text") or (sent.args[1] if len(sent.args) > 1 else "")
    assert "Андрей" in text_arg

    # DB: record with user 100 (4 wins after adjustment)
    async with sessionmaker() as session:
        row = (await session.execute(select(MonthlyChampion))).scalar_one()
        assert row.user_id == 100
        assert row.score == 4

    # chat_memories slot updated
    async with sessionmaker() as session:
        mem = await session.get(ChatMemory, chat_id)
        assert mem is not None
        assert mem.monthly_champion["user_id"] == 100
        assert mem.monthly_champion["title"] == "Мудак дня"


@pytest.mark.asyncio
async def test_e2e_idempotent_repeat(sessionmaker):
    chat_id = 42
    period_start = date(2026, 4, 1)
    period_end_excl = date(2026, 5, 1)

    async with sessionmaker() as session:
        session.add(Chat(id=chat_id, title="Test", is_active=True))
        session.add(MonthlyChampion(
            chat_id=chat_id, period_start=period_start,
            user_id=100, display_name="Андрей", score=5,
            tied_with=[], daily_title_snapshot="Мудак дня",
            announced_at=datetime(2026, 5, 1, 12, 0, 0),
        ))
        await session.commit()

    bot = AsyncMock()
    bot.send_message = AsyncMock()
    bot.get_chat_member = AsyncMock()

    roulette = create_autospec(RouletteService, instance=True)
    roulette._aggregate = AsyncMock()
    settings = create_autospec(SettingsService, instance=True)
    settings.get_all = AsyncMock(return_value={"is_active": True})
    app_config = create_autospec(AppConfigService, instance=True)
    app_config.get_all = AsyncMock(return_value={})

    svc = MonthlyChampionService(
        sessionmaker=sessionmaker, bot=bot,
        roulette=roulette, settings=settings, app_config=app_config,
    )

    await svc.process_chat(
        chat_id=chat_id, period_start=period_start, period_end_excl=period_end_excl,
    )

    bot.send_message.assert_not_awaited()
    roulette._aggregate.assert_not_awaited()
