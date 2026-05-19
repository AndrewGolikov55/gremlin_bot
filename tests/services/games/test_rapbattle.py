from __future__ import annotations

import unittest.mock as um
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, create_autospec

import pytest
from aiogram.enums import ChatMemberStatus
from sqlalchemy import select

from app.models import (
    Message,
    RapbattleRound,
    RouletteScoreAdjustment,
    User,
    UserMemoryProfile,
)
from app.services.app_config import AppConfigService
from app.services.games.rapbattle import ROUND_MAX_AGE, RapbattleService
from app.services.persona import StylePromptService
from app.services.settings import SettingsService


def _make_bot():
    bot = MagicMock()
    member = type("M", (), {})()
    member.status = ChatMemberStatus.MEMBER
    member.user = type("U", (), {})()
    member.user.first_name = "X"
    member.user.username = None
    member.user.is_bot = False
    bot.get_chat_member = AsyncMock(return_value=member)
    bot.send_message = AsyncMock(return_value=MagicMock(message_id=10))
    poll_msg = MagicMock(message_id=11, poll=MagicMock(id="POLL-RAP"))
    bot.send_poll = AsyncMock(return_value=poll_msg)
    return bot


def _make_svc(sessionmaker, *, bot=None):
    bot = bot or _make_bot()
    settings = create_autospec(SettingsService, instance=True)
    settings.get_all = AsyncMock(return_value={"style": "default"})
    personas = create_autospec(StylePromptService, instance=True)
    personas.get = AsyncMock(return_value="PERSONA")
    app_config = create_autospec(AppConfigService, instance=True)
    app_config.get_all = AsyncMock(return_value={})
    return RapbattleService(
        sessionmaker=sessionmaker,
        bot=bot,
        personas=personas,
        settings=settings,
        app_config=app_config,
    )


async def _seed_users(sessionmaker, chat_id=42):
    async with sessionmaker() as session:
        session.add(User(tg_id=100, username="andrew"))
        session.add(User(tg_id=200, username="ben"))
        for uid in (100, 200):
            for i in range(3):
                session.add(Message(
                    chat_id=chat_id, message_id=uid * 10 + i, user_id=uid,
                    text=f"msg-{uid}-{i}",
                    date=datetime.utcnow(),
                    is_bot=False, reply_to_id=None,
                    tg_file_id=None, media_group_id=None,
                ))
            session.add(UserMemoryProfile(
                chat_id=chat_id, user_id=uid,
                identity=["test"], preferences=[], projects=[], boundaries=[],
                summary="",
            ))
        await session.commit()


@pytest.mark.asyncio
async def test_start_with_unknown_opponent_refuses(sessionmaker):
    svc = _make_svc(sessionmaker)
    await svc.start(
        chat_id=42, initiator_id=100, opponent_arg="@nobody", opponent_reply_id=None,
    )
    async with sessionmaker() as session:
        rounds = (await session.execute(select(RapbattleRound))).scalars().all()
    assert rounds == []


@pytest.mark.asyncio
async def test_start_with_self_refuses(sessionmaker):
    svc = _make_svc(sessionmaker)
    await _seed_users(sessionmaker)
    await svc.start(
        chat_id=42, initiator_id=100, opponent_arg="@andrew", opponent_reply_id=None,
    )
    async with sessionmaker() as session:
        rounds = (await session.execute(select(RapbattleRound))).scalars().all()
    assert rounds == []


@pytest.mark.asyncio
async def test_start_happy_path_generates_round_and_opens_poll(sessionmaker):
    bot = _make_bot()
    svc = _make_svc(sessionmaker, bot=bot)
    await _seed_users(sessionmaker)
    with um.patch(
        "app.services.games.rapbattle.llm_generate",
        AsyncMock(return_value="строка1\nстрока2\nстрока3\nстрока4"),
    ):
        await svc.start(
            chat_id=42, initiator_id=100, opponent_arg="@ben", opponent_reply_id=None,
        )
    async with sessionmaker() as session:
        row = (await session.execute(select(RapbattleRound))).scalar_one()
    assert row.challenger_a_id == 100
    assert row.challenger_b_id == 200
    assert row.status == "voting"
    assert len(row.verses) == 4
    bot.send_poll.assert_awaited_once()


@pytest.mark.asyncio
async def test_resolve_after_winner_writes_roulette_adjustment(sessionmaker):
    bot = _make_bot()
    svc = _make_svc(sessionmaker, bot=bot)
    await _seed_users(sessionmaker)
    with um.patch(
        "app.services.games.rapbattle.llm_generate",
        AsyncMock(return_value="строка1\nстрока2\nстрока3\nстрока4"),
    ):
        await svc.start(
            chat_id=42, initiator_id=100, opponent_arg="@ben", opponent_reply_id=None,
        )
    async with sessionmaker() as session:
        round_id = (await session.execute(select(RapbattleRound.id))).scalar_one()

    # Simulate poll closed with A winning
    poll_a_wins = MagicMock()
    poll_a_wins.options = [MagicMock(voter_count=3), MagicMock(voter_count=1)]
    bot.stop_poll = AsyncMock(return_value=poll_a_wins)

    with um.patch("asyncio.sleep", AsyncMock(return_value=None)):
        await svc._resolve_after(
            chat_id=42, round_id=round_id, poll_message_id=11,
            a_id=100, b_id=200, a_display="andrew", b_display="ben",
        )

    async with sessionmaker() as session:
        row = (await session.execute(select(RapbattleRound))).scalar_one()
        adjustments = (await session.execute(select(RouletteScoreAdjustment))).scalars().all()
    assert row.status == "finished"
    assert row.winner_user_id == 100
    assert len(adjustments) == 1
    assert adjustments[0].user_id == 100
    assert adjustments[0].delta == -1
    assert adjustments[0].reason == "rapbattle_win"
    assert adjustments[0].source_id == round_id


@pytest.mark.asyncio
async def test_resolve_after_tie_no_adjustment(sessionmaker):
    bot = _make_bot()
    svc = _make_svc(sessionmaker, bot=bot)
    await _seed_users(sessionmaker)
    with um.patch(
        "app.services.games.rapbattle.llm_generate",
        AsyncMock(return_value="rap rap rap rap"),
    ):
        await svc.start(
            chat_id=42, initiator_id=100, opponent_arg="@ben", opponent_reply_id=None,
        )
    async with sessionmaker() as session:
        round_id = (await session.execute(select(RapbattleRound.id))).scalar_one()
    tie_poll = MagicMock()
    tie_poll.options = [MagicMock(voter_count=1), MagicMock(voter_count=1)]
    bot.stop_poll = AsyncMock(return_value=tie_poll)
    with um.patch("asyncio.sleep", AsyncMock(return_value=None)):
        await svc._resolve_after(
            chat_id=42, round_id=round_id, poll_message_id=11,
            a_id=100, b_id=200, a_display="a", b_display="b",
        )
    async with sessionmaker() as session:
        adjustments = (await session.execute(select(RouletteScoreAdjustment))).scalars().all()
    assert adjustments == []


@pytest.mark.asyncio
async def test_recover_stale_expires_stuck_voting(sessionmaker):
    svc = _make_svc(sessionmaker)
    await _seed_users(sessionmaker)
    with um.patch(
        "app.services.games.rapbattle.llm_generate",
        AsyncMock(return_value="rap rap rap rap"),
    ):
        await svc.start(
            chat_id=42, initiator_id=100, opponent_arg="@ben", opponent_reply_id=None,
        )
    # Backdate started_at
    async with sessionmaker() as session:
        row = (await session.execute(select(RapbattleRound))).scalar_one()
        row.started_at = datetime.utcnow() - ROUND_MAX_AGE - timedelta(minutes=5)
        await session.commit()
    recovered = await svc.recover_stale()
    assert recovered == 1
    async with sessionmaker() as session:
        row = (await session.execute(select(RapbattleRound))).scalar_one()
    assert row.status == "finished"


class TestRapbattleActiveSummary:
    @pytest.mark.asyncio
    async def test_returns_none_when_no_round(self, sessionmaker):
        svc = _make_svc(sessionmaker)
        result = await svc.get_active_summary(chat_id=42)
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_string_in_voting_state(self, sessionmaker):
        svc = _make_svc(sessionmaker)
        await _seed_users(sessionmaker)
        with um.patch(
            "app.services.games.rapbattle.llm_generate",
            AsyncMock(return_value="rap rap rap rap"),
        ):
            await svc.start(
                chat_id=42, initiator_id=100, opponent_arg="@ben",
                opponent_reply_id=None,
            )
        # After start(), round is in VOTING state
        result = await svc.get_active_summary(chat_id=42)
        assert result is not None
        assert "🎤 Rapbattle" in result
        assert "голосование" in result
