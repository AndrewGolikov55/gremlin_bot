from __future__ import annotations

import json

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.app_setting import AppSetting
from app.services.app_config import AppConfigService
from tests.fakes import FakeRedis


async def test_get_all_returns_defaults(
    sessionmaker: async_sessionmaker[AsyncSession], fake_redis: FakeRedis
) -> None:
    service = AppConfigService(sessionmaker, fake_redis)

    initial = await service.get_all()

    assert initial["llm_provider"] == "openrouter"
    assert initial["context_max_turns"] == 100


async def test_get_all_reads_from_cache(
    sessionmaker: async_sessionmaker[AsyncSession], fake_redis: FakeRedis
) -> None:
    service = AppConfigService(sessionmaker, fake_redis)
    payload = {"llm_provider": "openai", "context_max_turns": 12}
    await fake_redis.set("app:settings", json.dumps(payload, ensure_ascii=False))

    data = await service.get_all()

    assert data == payload


async def test_set_invalidates_cache_before_repopulation(
    sessionmaker: async_sessionmaker[AsyncSession], fake_redis: FakeRedis
) -> None:
    service = AppConfigService(sessionmaker, fake_redis)
    await fake_redis.set("app:settings", json.dumps({"llm_provider": "openai"}, ensure_ascii=False))

    await service.set("llm_provider", "anthropic")

    assert await fake_redis.get("app:settings") is None

    async with sessionmaker() as session:
        row = await session.get(AppSetting, "llm_provider")
        assert row is not None
        assert row.value == "anthropic"

    refreshed = await service.get_all()
    assert refreshed["llm_provider"] == "anthropic"
