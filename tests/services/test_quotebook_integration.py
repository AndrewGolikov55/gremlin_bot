from __future__ import annotations

from datetime import date, datetime, timedelta
from unittest.mock import AsyncMock, create_autospec

import pytest
from aiogram.exceptions import TelegramBadRequest
from sqlalchemy import select

from app.models import Chat, Message, QuoteWeekRound, RouletteScoreAdjustment
from app.services.app_config import AppConfigService
from app.services.quotebook import QuotebookService
from app.services.settings import SettingsService


def _make_bot(*, send_poll_returns: object) -> AsyncMock:
    bot = AsyncMock()
    me = type("Me", (), {})()
    me.username = "gremlin_bot"
    bot.get_me = AsyncMock(return_value=me)
    bot.send_poll = AsyncMock(return_value=send_poll_returns)
    bot.send_message = AsyncMock()
    bot.get_chat_member = AsyncMock(
        side_effect=TelegramBadRequest(method=None, message="x")  # type: ignore[arg-type]
    )
    return bot


def _poll_msg(*, message_id: int, poll_id: str) -> object:
    pm = type("PM", (), {})()
    pm.message_id = message_id
    pm.poll = type("P", (), {})()
    pm.poll.id = poll_id
    return pm


def _stub_close(voter_counts: list[int]) -> object:
    poll = type("P", (), {})()
    poll.total_voter_count = sum(voter_counts)
    opts = []
    for c in voter_counts:
        o = type("O", (), {})()
        o.voter_count = c
        opts.append(o)
    poll.options = opts
    return poll


@pytest.mark.asyncio
async def test_e2e_first_sunday_publishes_no_close(sessionmaker, monkeypatch):
    chat_id = 42
    now = datetime(2026, 5, 17, 20, 0, 0)  # воскресенье

    async with sessionmaker() as session:
        session.add(Chat(id=chat_id, title="T", is_active=True))
        for i in range(1, 6):
            session.add(Message(
                chat_id=chat_id, message_id=i, user_id=100 + i,
                text=f"цитата номер {i} с нормальной длиной",
                is_bot=False, date=now - timedelta(days=2),
            ))
        await session.commit()

    bot = _make_bot(send_poll_returns=_poll_msg(message_id=999, poll_id="poll-1"))
    settings = create_autospec(SettingsService, instance=True)
    settings.get_all = AsyncMock(return_value={"is_active": True})
    app_config = create_autospec(AppConfigService, instance=True)
    app_config.get_all = AsyncMock(return_value={})

    svc = QuotebookService(
        sessionmaker=sessionmaker, bot=bot,
        settings=settings, app_config=app_config,
    )

    async def fake_gen(messages, **kw):
        return "x"
    monkeypatch.setattr("app.services.quotebook.llm_generate", fake_gen)

    await svc.process_chat(chat_id=chat_id, now=now)

    # send_poll вызван 1 раз
    bot.send_poll.assert_awaited_once()
    # Объявление-победитель не публиковалось (нет старого раунда)
    bot.send_message.assert_not_awaited()

    async with sessionmaker() as session:
        rows = (await session.execute(select(QuoteWeekRound))).scalars().all()
        assert len(rows) == 1
        assert rows[0].poll_id == "poll-1"
        assert rows[0].closed_at is None


@pytest.mark.asyncio
async def test_e2e_second_sunday_closes_old_publishes_new_and_awards_plus_one(sessionmaker, monkeypatch):
    chat_id = 42
    sun1 = datetime(2026, 5, 17, 20, 0, 0)
    sun2 = datetime(2026, 5, 24, 20, 0, 0)

    # 1) Подготовим состояние «прошлая неделя уже опубликована»
    async with sessionmaker() as session:
        session.add(Chat(id=chat_id, title="T", is_active=True))
        session.add(QuoteWeekRound(
            chat_id=chat_id, week_start=date(2026, 5, 4),
            poll_id="prev", poll_message_id=10,
            options=[
                {"text": "первая прошлая", "author_user_id": 100, "source_message_id": 1},
                {"text": "вторая прошлая", "author_user_id": 101, "source_message_id": 2},
            ],
            opened_at=sun1,
        ))
        # Сообщения для НОВОГО раунда (за окно sun2 - 7d, sun2)
        for i in range(1, 5):
            session.add(Message(
                chat_id=chat_id, message_id=200 + i, user_id=200 + i,
                text=f"новая цитата {i} нормальной длины да",
                is_bot=False, date=sun2 - timedelta(days=1),
            ))
        await session.commit()

    bot = _make_bot(send_poll_returns=_poll_msg(message_id=555, poll_id="poll-2"))
    bot.stop_poll = AsyncMock(return_value=_stub_close([0, 2]))  # победил idx=1 (user 101)
    settings = create_autospec(SettingsService, instance=True)
    settings.get_all = AsyncMock(return_value={"is_active": True})
    app_config = create_autospec(AppConfigService, instance=True)
    app_config.get_all = AsyncMock(return_value={})

    svc = QuotebookService(
        sessionmaker=sessionmaker, bot=bot,
        settings=settings, app_config=app_config,
    )

    async def fake_gen(messages, **kw):
        return "📜 Афоризм недели — вторая прошлая, от id101."
    monkeypatch.setattr("app.services.quotebook.llm_generate", fake_gen)

    await svc.process_chat(chat_id=chat_id, now=sun2)

    # Закрыли старый poll
    bot.stop_poll.assert_awaited_once_with(chat_id=chat_id, message_id=10)
    # Открыли новый poll
    bot.send_poll.assert_awaited_once()
    # Объявили победителя — 1 send_message
    bot.send_message.assert_awaited_once()

    async with sessionmaker() as session:
        # Order by id (insertion order): older row was inserted first so has lower id.
        # opened_at is unreliable for ordering here because the new round's opened_at
        # is set from datetime.utcnow() (real wall clock) while the fixture sets the
        # old round's opened_at to a fixed 2026-05-17 value.
        rows = (await session.execute(
            select(QuoteWeekRound).order_by(QuoteWeekRound.id)
        )).scalars().all()
        assert len(rows) == 2
        old, new = rows
        assert old.poll_id == "prev"
        assert old.closed_at is not None
        assert old.winner_user_id == 101
        assert old.winner_option_idx == 1
        assert old.final_counts == [0, 2]
        assert new.poll_id == "poll-2"
        assert new.closed_at is None

        adj = (await session.execute(select(RouletteScoreAdjustment))).scalars().all()
        assert len(adj) == 1
        assert adj[0].user_id == 101
        assert adj[0].delta == 1
        assert adj[0].reason == "quote_week_winner"
        assert adj[0].source_id == old.id
