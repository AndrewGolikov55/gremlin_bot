from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.chat import Chat, ChatSetting
from app.services.settings import SettingsService
from tests.fakes import FakeRedis


async def test_get_returns_default_on_empty_storage(
    sessionmaker: async_sessionmaker[AsyncSession], fake_redis: FakeRedis
) -> None:
    service = SettingsService(sessionmaker, fake_redis)

    assert await service.get(10, "style") == "gopnik"


async def test_set_creates_chat_and_invalidates_cache(
    sessionmaker: async_sessionmaker[AsyncSession], fake_redis: FakeRedis
) -> None:
    service = SettingsService(sessionmaker, fake_redis)

    await fake_redis.set("chat:10:setting:style", json.dumps("boss", ensure_ascii=False))
    await service.set(10, "style", "jarvis")

    async with sessionmaker() as session:
        chat = await session.get(Chat, 10)
        row = (
            await session.execute(
                select(ChatSetting).where(ChatSetting.chat_id == 10, ChatSetting.key == "style")
            )
        ).scalar_one()

    assert chat is not None
    assert row.value == "jarvis"
    assert await fake_redis.get("chat:10:setting:style") is None
    assert await service.get(10, "style") == "jarvis"
