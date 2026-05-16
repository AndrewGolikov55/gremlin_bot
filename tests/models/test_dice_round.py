from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import DiceRound


@pytest.mark.asyncio
async def test_dice_round_roundtrip(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    async with sessionmaker() as session:
        row = DiceRound(
            chat_id=-100,
            user_id=42,
            picks=[3, 5],
            dice_value=5,
            won=True,
            delta=-1,
            rolled_at=datetime.utcnow(),
            dice_message_id=999,
        )
        session.add(row)
        await session.commit()

    async with sessionmaker() as session:
        loaded = (await session.execute(select(DiceRound))).scalar_one()
        assert loaded.chat_id == -100
        assert loaded.user_id == 42
        assert loaded.picks == [3, 5]
        assert loaded.dice_value == 5
        assert loaded.won is True
        assert loaded.delta == -1
        assert loaded.dice_message_id == 999
