from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import RouletteScoreAdjustment, RouletteWinner
from tests.services.test_roulette import build_service


@pytest.mark.asyncio
async def test_monthly_aggregate_subtracts_adjustments(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    chat_id = -100
    today = date.today()
    month_start = today.replace(day=1)

    async with sessionmaker() as session:
        for d_offset in range(3):
            session.add(RouletteWinner(
                chat_id=chat_id, user_id=1, username="alice",
                title_code="boss", title="Босс",
                won_at=month_start + timedelta(days=d_offset),
            ))
        session.add(RouletteScoreAdjustment(
            chat_id=chat_id, user_id=1, delta=-1, reason="guess_first_winner",
            created_at=datetime.utcnow(),
        ))
        await session.commit()

    svc = build_service(sessionmaker)
    async with sessionmaker() as session:
        results = await svc._aggregate(session, chat_id, start=month_start)

    assert len(results) == 1
    assert results[0].user_id == 1
    assert results[0].wins == 2  # 3 wins − 1


@pytest.mark.asyncio
async def test_aggregate_hides_users_with_nonpositive_score(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    chat_id = -100
    async with sessionmaker() as session:
        session.add(RouletteScoreAdjustment(
            chat_id=chat_id, user_id=2, delta=-1, reason="guess_first_winner",
            created_at=datetime.utcnow(),
        ))
        await session.commit()

    svc = build_service(sessionmaker)
    async with sessionmaker() as session:
        results = await svc._aggregate(session, chat_id, start=None)

    assert results == []


@pytest.mark.asyncio
async def test_monthly_aggregate_ignores_old_adjustments(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    chat_id = -100
    today = date.today()
    month_start = today.replace(day=1)
    last_month = month_start - timedelta(days=15)

    async with sessionmaker() as session:
        session.add(RouletteWinner(
            chat_id=chat_id, user_id=3, username="bob",
            title_code="boss", title="Босс",
            won_at=month_start,
        ))
        session.add(RouletteScoreAdjustment(
            chat_id=chat_id, user_id=3, delta=-1, reason="guess_first_winner",
            created_at=datetime.combine(last_month, datetime.min.time()),
        ))
        await session.commit()

    svc = build_service(sessionmaker)
    async with sessionmaker() as session:
        results = await svc._aggregate(session, chat_id, start=month_start)

    assert len(results) == 1
    assert results[0].wins == 1
