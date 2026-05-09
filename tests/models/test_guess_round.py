from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import GuessRound, RouletteScoreAdjustment


@pytest.mark.asyncio
async def test_guess_round_round_trip(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    async with sessionmaker() as session:
        row = GuessRound(
            chat_id=-100,
            poll_id="poll-1",
            chat_message_id=42,
            source_chat_id=-100,
            source_message_id=10,
            author_user_id=555,
            correct_option_id=2,
            option_user_ids=[111, 222, 333, 555],
            started_at=datetime(2026, 5, 9, 10, 0, 0),
            selection_mode="llm",
        )
        session.add(row)
        await session.commit()
        assert row.id is not None
        assert row.first_winner_user_id is None


@pytest.mark.asyncio
async def test_roulette_score_adjustment_round_trip(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    async with sessionmaker() as session:
        adj = RouletteScoreAdjustment(
            chat_id=-100,
            user_id=42,
            delta=-1,
            reason="guess_first_winner",
            source_id=7,
        )
        session.add(adj)
        await session.commit()
        assert adj.id is not None
        assert adj.created_at is not None
