from __future__ import annotations

from datetime import date, datetime

import pytest
from sqlalchemy import select

from app.models import ChatMemory, MonthlyChampion


@pytest.mark.asyncio
async def test_monthly_champion_round_trip(sessionmaker):
    async with sessionmaker() as session:
        row = MonthlyChampion(
            chat_id=42,
            period_start=date(2026, 4, 1),
            user_id=100,
            display_name="Андрей",
            score=7,
            tied_with=[],
            daily_title_snapshot="Мудак дня",
            announced_at=datetime(2026, 5, 1, 9, 0, 0),
        )
        session.add(row)
        await session.commit()

        loaded = (await session.execute(select(MonthlyChampion))).scalar_one()
        assert loaded.chat_id == 42
        assert loaded.user_id == 100
        assert loaded.display_name == "Андрей"
        assert loaded.score == 7
        assert loaded.tied_with == []
        assert loaded.daily_title_snapshot == "Мудак дня"


@pytest.mark.asyncio
async def test_monthly_champion_nullable_user(sessionmaker):
    async with sessionmaker() as session:
        row = MonthlyChampion(
            chat_id=42,
            period_start=date(2026, 4, 1),
            user_id=None,
            display_name=None,
            score=0,
            tied_with=[],
            daily_title_snapshot="Мудак дня",
            announced_at=datetime(2026, 5, 1, 9, 0, 0),
        )
        session.add(row)
        await session.commit()
        loaded = (await session.execute(select(MonthlyChampion))).scalar_one()
        assert loaded.user_id is None


@pytest.mark.asyncio
async def test_chat_memory_has_monthly_champion_slot(sessionmaker):
    async with sessionmaker() as session:
        mem = ChatMemory(
            chat_id=42,
            members=[],
            lore=[],
            monthly_champion={
                "user_id": 100,
                "display_name": "Андрей",
                "title": "Король Мудаков",
                "period_start": "2026-04-01",
            },
        )
        session.add(mem)
        await session.commit()

        loaded = (await session.execute(select(ChatMemory))).scalar_one()
        assert loaded.monthly_champion["user_id"] == 100
        assert loaded.monthly_champion["title"] == "Король Мудаков"
