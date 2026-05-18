from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from aiogram.enums import ChatMemberStatus
from sqlalchemy import select

from app.models import SpyPlayer, SpyRound
from app.services.games.spy import SpyService


def _make_bot():
    bot = AsyncMock()
    member = type("M", (), {})()
    member.status = ChatMemberStatus.MEMBER
    member.user = type("U", (), {})()
    member.user.first_name = "Игрок"
    member.user.username = None
    member.user.is_bot = False
    bot.get_chat_member = AsyncMock(return_value=member)
    bot.send_message = AsyncMock()
    return bot


@pytest.mark.asyncio
async def test_start_lobby_creates_round_with_initiator_as_player(sessionmaker):
    bot = _make_bot()
    svc = SpyService(sessionmaker=sessionmaker, bot=bot)
    await svc.start_lobby(chat_id=42, initiator_id=100)

    async with sessionmaker() as session:
        rounds = (await session.execute(select(SpyRound))).scalars().all()
        players = (await session.execute(select(SpyPlayer))).scalars().all()
    assert len(rounds) == 1
    assert rounds[0].status == "lobby"
    assert len(players) == 1
    assert players[0].user_id == 100


@pytest.mark.asyncio
async def test_join_adds_player(sessionmaker):
    bot = _make_bot()
    svc = SpyService(sessionmaker=sessionmaker, bot=bot)
    await svc.start_lobby(chat_id=42, initiator_id=100)
    await svc.join(chat_id=42, user_id=101)

    async with sessionmaker() as session:
        players = (await session.execute(select(SpyPlayer))).scalars().all()
    assert sorted(p.user_id for p in players) == [100, 101]


@pytest.mark.asyncio
async def test_start_round_requires_min_players(sessionmaker):
    bot = _make_bot()
    svc = SpyService(sessionmaker=sessionmaker, bot=bot)
    await svc.start_lobby(chat_id=42, initiator_id=100)
    await svc.join(chat_id=42, user_id=101)
    await svc.start_round(chat_id=42, initiator_id=100)

    async with sessionmaker() as session:
        round_ = (await session.execute(select(SpyRound))).scalars().one()
    # Still lobby — only 2 players
    assert round_.status == "lobby"


@pytest.mark.asyncio
async def test_reveal_role_returns_location_or_spy(sessionmaker):
    bot = _make_bot()
    svc = SpyService(sessionmaker=sessionmaker, bot=bot)
    await svc.start_lobby(chat_id=42, initiator_id=100)
    await svc.join(chat_id=42, user_id=101)
    await svc.join(chat_id=42, user_id=102)
    await svc.start_round(chat_id=42, initiator_id=100)

    async with sessionmaker() as session:
        round_ = (await session.execute(select(SpyRound))).scalars().one()

    for uid in (100, 101, 102):
        text, found = await svc.reveal_role(chat_id=42, user_id=uid, round_id=round_.id)
        assert found is True
        if uid == round_.spy_user_id:
            assert "ШПИОН" in text
        else:
            assert round_.location in text
