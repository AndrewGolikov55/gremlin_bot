from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import select

from app.models import RoastRun


@pytest.mark.asyncio
async def test_roast_run_round_trip(sessionmaker):
    async with sessionmaker() as session:
        row = RoastRun(
            chat_id=42,
            target_user_id=100,
            initiator_user_id=200,
            target_username="andrew",
            run_at=datetime(2026, 5, 16, 12, 0, 0),
        )
        session.add(row)
        await session.commit()

        loaded = (await session.execute(select(RoastRun))).scalar_one()
        assert loaded.chat_id == 42
        assert loaded.target_user_id == 100
        assert loaded.initiator_user_id == 200
        assert loaded.target_username == "andrew"
        assert loaded.run_at == datetime(2026, 5, 16, 12, 0, 0)


@pytest.mark.asyncio
async def test_roast_run_nullable_username(sessionmaker):
    async with sessionmaker() as session:
        row = RoastRun(
            chat_id=42,
            target_user_id=100,
            initiator_user_id=200,
            target_username=None,
            run_at=datetime(2026, 5, 16, 12, 0, 0),
        )
        session.add(row)
        await session.commit()
        loaded = (await session.execute(select(RoastRun))).scalar_one()
        assert loaded.target_username is None
