from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.models import ShipResult


@pytest.mark.asyncio
async def test_ship_result_round_trip(sessionmaker):
    async with sessionmaker() as session:
        row = ShipResult(
            chat_id=42,
            user_id_a=100,
            user_id_b=200,
            score=73,
            payload={"reply_rate": 0.4, "mention_rate": 0.1, "co_activity": 0.6, "pref_overlap": 0.5},
            rendered_text="💞 73/100. Шипперим осторожно.",
            computed_at=datetime(2026, 5, 16, 12, 0, 0),
        )
        session.add(row)
        await session.commit()

        loaded = (await session.execute(select(ShipResult))).scalar_one()
        assert loaded.chat_id == 42
        assert loaded.user_id_a == 100
        assert loaded.user_id_b == 200
        assert loaded.score == 73
        assert loaded.payload["reply_rate"] == 0.4
        assert loaded.rendered_text.startswith("💞")


@pytest.mark.asyncio
async def test_ship_result_unique_pair_per_chat(sessionmaker):
    async with sessionmaker() as session:
        session.add(ShipResult(
            chat_id=42, user_id_a=100, user_id_b=200,
            score=50, payload={}, rendered_text="t1",
            computed_at=datetime(2026, 5, 16, 10, 0, 0),
        ))
        await session.commit()

    async with sessionmaker() as session:
        session.add(ShipResult(
            chat_id=42, user_id_a=100, user_id_b=200,
            score=60, payload={}, rendered_text="t2",
            computed_at=datetime(2026, 5, 16, 11, 0, 0),
        ))
        with pytest.raises(IntegrityError):
            await session.commit()


@pytest.mark.asyncio
async def test_ship_result_same_pair_different_chats_allowed(sessionmaker):
    async with sessionmaker() as session:
        session.add(ShipResult(
            chat_id=42, user_id_a=100, user_id_b=200,
            score=50, payload={}, rendered_text="t1",
            computed_at=datetime(2026, 5, 16, 10, 0, 0),
        ))
        session.add(ShipResult(
            chat_id=43, user_id_a=100, user_id_b=200,
            score=80, payload={}, rendered_text="t2",
            computed_at=datetime(2026, 5, 16, 10, 0, 0),
        ))
        await session.commit()

        rows = (await session.execute(select(ShipResult))).scalars().all()
        assert len(rows) == 2
