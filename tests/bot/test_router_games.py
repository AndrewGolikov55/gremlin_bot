from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.types import Chat, PollAnswer
from aiogram.types import User as TgUser
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot.router_games import (
    _start_round,
    build_games_menu_markup,
    format_first_winner_message,
    on_poll_answer,
)
from app.models import GuessRound, Message, RouletteScoreAdjustment
from app.services.guess_game import GuessGameService, LLMPick


def test_build_games_menu_returns_inline_keyboard_with_guess() -> None:
    markup = build_games_menu_markup()
    flat = [btn for row in markup.inline_keyboard for btn in row]
    assert any(btn.callback_data == "games:guess" for btn in flat)
    assert any("Угадай" in btn.text for btn in flat)


def test_format_first_winner_message_mentions_user_and_penalty() -> None:
    msg = format_first_winner_message(display_name="Андрей", username="andrey")
    assert "Андрей" in msg or "@andrey" in msg
    assert "1 очко" in msg


def test_format_first_winner_message_falls_back_to_display_name_without_username() -> None:
    msg = format_first_winner_message(display_name="Bob", username=None)
    assert "Bob" in msg
    assert "@" not in msg


# ---------------------------------------------------------------------------
# Integration test helpers
# ---------------------------------------------------------------------------

async def _seed_messages(sessionmaker_: async_sessionmaker[AsyncSession], chat_id: int) -> None:
    base = "это какой-то длинный тестовый текст для опроса " * 2
    async with sessionmaker_() as session:
        for uid in (1, 2, 3, 4):
            for i in range(5):
                session.add(Message(
                    chat_id=chat_id, message_id=uid * 100 + i, user_id=uid,
                    text=base + str(uid) + "-" + str(i),
                    reply_to_id=None,
                    date=datetime.utcnow() - timedelta(days=2),
                    is_bot=False, tg_file_id=None, media_group_id=None,
                ))
        await session.commit()


def _make_svc(sessionmaker_: async_sessionmaker[AsyncSession], *, llm_user_id: int = 2, llm_message_id: int = 200) -> GuessGameService:
    app_config = MagicMock()
    app_config.get_all = AsyncMock(return_value={})

    async def fake_llm_pick(*args, **kwargs):
        return LLMPick(author_user_id=llm_user_id, message_id=llm_message_id, reason="ok")

    async def fake_display_name(chat_id_, user_id_):
        return f"User{user_id_}"

    return GuessGameService(
        sessionmaker=sessionmaker_,
        app_config=app_config,
        bot=None,
        display_name=fake_display_name,
        llm_pick=fake_llm_pick,
    )


def _fake_bot_with_poll(poll_id: str = "POLL-XYZ") -> MagicMock:
    bot = MagicMock()
    sent_poll = MagicMock()
    sent_poll.message_id = 999
    sent_poll.poll = MagicMock(id=poll_id)
    bot.send_poll = AsyncMock(return_value=sent_poll)
    bot.send_message = AsyncMock()
    return bot


# ---------------------------------------------------------------------------
# Integration tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_full_flow_first_winner_gets_adjustment(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    chat_id = -400
    await _seed_messages(sessionmaker, chat_id)
    svc = _make_svc(sessionmaker)
    bot = _fake_bot_with_poll("POLL-1")

    chat = Chat(id=chat_id, type="supergroup")
    await _start_round(chat, bot, svc)

    async with sessionmaker() as session:
        rounds = (await session.execute(select(GuessRound))).scalars().all()
        assert len(rounds) == 1
        assert rounds[0].author_user_id == 2
        assert rounds[0].poll_id == "POLL-1"
        round_id = rounds[0].id
        correct_id = rounds[0].correct_option_id

    answer = PollAnswer(
        poll_id="POLL-1",
        user=TgUser(id=42, is_bot=False, first_name="Hero", username="hero"),
        option_ids=[correct_id],
        option_persistent_ids=[],
    )
    await on_poll_answer(answer, bot, svc)

    async with sessionmaker() as session:
        adj = (await session.execute(
            select(RouletteScoreAdjustment).where(RouletteScoreAdjustment.user_id == 42)
        )).scalar_one()
        assert adj.delta == -1
        assert adj.source_id == round_id
        rnd = (await session.execute(select(GuessRound).where(GuessRound.id == round_id))).scalar_one()
        assert rnd.first_winner_user_id == 42

    bot.send_message.assert_called()


@pytest.mark.asyncio
async def test_second_correct_vote_no_extra_adjustment(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    chat_id = -401
    await _seed_messages(sessionmaker, chat_id)
    svc = _make_svc(sessionmaker)
    bot = _fake_bot_with_poll("POLL-2")

    await _start_round(Chat(id=chat_id, type="supergroup"), bot, svc)

    async with sessionmaker() as session:
        rnd = (await session.execute(select(GuessRound))).scalar_one()
        correct_id = rnd.correct_option_id

    a1 = PollAnswer(poll_id="POLL-2", user=TgUser(id=42, is_bot=False, first_name="A"), option_ids=[correct_id], option_persistent_ids=[])
    a2 = PollAnswer(poll_id="POLL-2", user=TgUser(id=43, is_bot=False, first_name="B"), option_ids=[correct_id], option_persistent_ids=[])
    await on_poll_answer(a1, bot, svc)
    await on_poll_answer(a2, bot, svc)

    async with sessionmaker() as session:
        adjs = (await session.execute(select(RouletteScoreAdjustment))).scalars().all()
        assert len(adjs) == 1
        assert adjs[0].user_id == 42


@pytest.mark.asyncio
async def test_daily_limit_blocks_second_start(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    chat_id = -402
    await _seed_messages(sessionmaker, chat_id)
    svc = _make_svc(sessionmaker)
    bot = _fake_bot_with_poll("POLL-3")

    chat = Chat(id=chat_id, type="supergroup")
    await _start_round(chat, bot, svc)
    bot.send_message.reset_mock()
    bot.send_poll.reset_mock()

    await _start_round(chat, bot, svc)

    bot.send_poll.assert_not_called()
    bot.send_message.assert_called_once()
    args, kwargs = bot.send_message.call_args
    text = kwargs.get("text") or (args[1] if len(args) > 1 else "")
    assert "уже играли" in text.lower()
