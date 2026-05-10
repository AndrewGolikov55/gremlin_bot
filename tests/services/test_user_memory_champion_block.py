from __future__ import annotations

import pytest

from app.models import ChatMemory
from app.services.user_memory import UserMemoryService


@pytest.mark.asyncio
async def test_champion_block_returns_none_when_slot_empty(sessionmaker):
    chat_id = 42
    async with sessionmaker() as session:
        session.add(ChatMemory(chat_id=chat_id, members=[], lore=[], monthly_champion=None))
        await session.commit()

    svc = UserMemoryService.__new__(UserMemoryService)
    async with sessionmaker() as session:
        block = await svc.build_monthly_champion_block(session, chat_id=chat_id)
    assert block is None


@pytest.mark.asyncio
async def test_champion_block_returns_none_when_chat_memory_missing(sessionmaker):
    svc = UserMemoryService.__new__(UserMemoryService)
    async with sessionmaker() as session:
        block = await svc.build_monthly_champion_block(session, chat_id=999)
    assert block is None


@pytest.mark.asyncio
async def test_champion_block_returns_string_with_title(sessionmaker):
    chat_id = 42
    async with sessionmaker() as session:
        session.add(ChatMemory(
            chat_id=chat_id,
            members=[],
            lore=[],
            monthly_champion={
                "user_id": 100,
                "display_name": "Андрей",
                "title": "Король Мудаков",
                "period_start": "2026-04-01",
            },
        ))
        await session.commit()

    svc = UserMemoryService.__new__(UserMemoryService)
    async with sessionmaker() as session:
        block = await svc.build_monthly_champion_block(session, chat_id=chat_id)

    assert block is not None
    assert "Король Мудаков" in block
    assert "Андрей" in block


@pytest.mark.asyncio
async def test_champion_block_handles_malformed_period_start(sessionmaker):
    chat_id = 42
    async with sessionmaker() as session:
        session.add(ChatMemory(
            chat_id=chat_id,
            members=[],
            lore=[],
            monthly_champion={
                "user_id": 100,
                "display_name": "Андрей",
                "title": "Король Мудаков",
                "period_start": "not-a-date",
            },
        ))
        await session.commit()

    svc = UserMemoryService.__new__(UserMemoryService)
    async with sessionmaker() as session:
        block = await svc.build_monthly_champion_block(session, chat_id=chat_id)

    assert block is not None
    assert "Король Мудаков" in block
    assert "Андрей" in block
