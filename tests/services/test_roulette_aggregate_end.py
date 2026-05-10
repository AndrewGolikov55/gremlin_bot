from __future__ import annotations

from datetime import date, datetime

import pytest

from app.models import RouletteScoreAdjustment, RouletteWinner
from app.services.roulette import RouletteService


@pytest.mark.asyncio
async def test_aggregate_end_only_window(sessionmaker):
    chat_id = 42
    async with sessionmaker() as session:
        # 2 wins в апреле
        session.add(RouletteWinner(
            chat_id=chat_id, user_id=1, username="Alice",
            won_at=date(2026, 4, 5), title_code="boss", title="t",
            created_at=datetime(2026, 4, 5, 10, 0, 0),
        ))
        session.add(RouletteWinner(
            chat_id=chat_id, user_id=1, username="Alice",
            won_at=date(2026, 4, 25), title_code="boss", title="t",
            created_at=datetime(2026, 4, 25, 10, 0, 0),
        ))
        # 1 win в мае — должен быть отфильтрован
        session.add(RouletteWinner(
            chat_id=chat_id, user_id=1, username="Alice",
            won_at=date(2026, 5, 3), title_code="boss", title="t",
            created_at=datetime(2026, 5, 3, 10, 0, 0),
        ))
        await session.commit()

    svc = RouletteService.__new__(RouletteService)
    async with sessionmaker() as session:
        result = await svc._aggregate(
            session,
            chat_id,
            start=date(2026, 4, 1),
            end=date(2026, 5, 1),
        )

    assert len(result) == 1
    assert result[0].user_id == 1
    assert result[0].wins == 2  # только апрельские


@pytest.mark.asyncio
async def test_aggregate_end_none_means_no_upper_bound(sessionmaker):
    chat_id = 42
    async with sessionmaker() as session:
        session.add(RouletteWinner(
            chat_id=chat_id, user_id=1, username="Alice",
            won_at=date(2026, 4, 5), title_code="boss", title="t",
            created_at=datetime(2026, 4, 5, 10, 0, 0),
        ))
        session.add(RouletteWinner(
            chat_id=chat_id, user_id=1, username="Alice",
            won_at=date(2026, 5, 3), title_code="boss", title="t",
            created_at=datetime(2026, 5, 3, 10, 0, 0),
        ))
        await session.commit()

    svc = RouletteService.__new__(RouletteService)
    async with sessionmaker() as session:
        result = await svc._aggregate(session, chat_id, start=date(2026, 4, 1))

    assert len(result) == 1
    assert result[0].wins == 2


@pytest.mark.asyncio
async def test_aggregate_end_filters_adjustments(sessionmaker):
    """end must filter RouletteScoreAdjustment.created_at as well."""
    chat_id = 42
    async with sessionmaker() as session:
        # Win в апреле
        session.add(RouletteWinner(
            chat_id=chat_id, user_id=1, username="Alice",
            won_at=date(2026, 4, 5), title_code="boss", title="t",
            created_at=datetime(2026, 4, 5, 10, 0, 0),
        ))
        # Adjustment в мае (вне окна) — не должен учитываться
        session.add(RouletteScoreAdjustment(
            chat_id=chat_id, user_id=1, delta=-1, reason="guess",
            created_at=datetime(2026, 5, 5, 10, 0, 0),
        ))
        await session.commit()

    svc = RouletteService.__new__(RouletteService)
    async with sessionmaker() as session:
        result = await svc._aggregate(
            session, chat_id, start=date(2026, 4, 1), end=date(2026, 5, 1),
        )

    assert len(result) == 1
    assert result[0].wins == 1  # adjustment отфильтрован
