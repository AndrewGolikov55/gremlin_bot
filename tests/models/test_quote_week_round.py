from __future__ import annotations

from datetime import date, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.models import QuoteWeekRound


@pytest.mark.asyncio
async def test_quote_week_round_round_trip(sessionmaker):
    async with sessionmaker() as session:
        row = QuoteWeekRound(
            chat_id=42,
            week_start=date(2026, 5, 11),
            poll_id="poll-abc",
            poll_message_id=999,
            options=[
                {"text": "цитата раз", "author_user_id": 100, "source_message_id": 1},
                {"text": "цитата два", "author_user_id": 101, "source_message_id": 2},
            ],
            opened_at=datetime(2026, 5, 17, 17, 0, 0),
        )
        session.add(row)
        await session.commit()

        loaded = (await session.execute(select(QuoteWeekRound))).scalar_one()
        assert loaded.chat_id == 42
        assert loaded.week_start == date(2026, 5, 11)
        assert loaded.poll_id == "poll-abc"
        assert loaded.poll_message_id == 999
        assert len(loaded.options) == 2
        assert loaded.options[0]["author_user_id"] == 100
        assert loaded.closed_at is None
        assert loaded.winner_user_id is None
        assert loaded.winner_option_idx is None
        assert loaded.final_counts is None


@pytest.mark.asyncio
async def test_quote_week_round_unique_chat_week(sessionmaker):
    async with sessionmaker() as session:
        session.add(QuoteWeekRound(
            chat_id=42, week_start=date(2026, 5, 11),
            poll_id="poll-a", poll_message_id=1, options=[],
            opened_at=datetime(2026, 5, 17, 17, 0, 0),
        ))
        await session.commit()

    async with sessionmaker() as session:
        session.add(QuoteWeekRound(
            chat_id=42, week_start=date(2026, 5, 11),  # тот же chat+week
            poll_id="poll-b", poll_message_id=2, options=[],
            opened_at=datetime(2026, 5, 17, 18, 0, 0),
        ))
        with pytest.raises(IntegrityError):
            await session.commit()


@pytest.mark.asyncio
async def test_quote_week_round_closed_with_winner(sessionmaker):
    async with sessionmaker() as session:
        row = QuoteWeekRound(
            chat_id=42, week_start=date(2026, 5, 11),
            poll_id="poll-x", poll_message_id=10,
            options=[{"text": "x", "author_user_id": 100, "source_message_id": 1}],
            opened_at=datetime(2026, 5, 17, 17, 0, 0),
            closed_at=datetime(2026, 5, 18, 17, 0, 0),
            winner_user_id=100,
            winner_option_idx=0,
            final_counts=[3],
        )
        session.add(row)
        await session.commit()

        loaded = (await session.execute(select(QuoteWeekRound))).scalar_one()
        assert loaded.winner_user_id == 100
        assert loaded.winner_option_idx == 0
        assert loaded.final_counts == [3]
