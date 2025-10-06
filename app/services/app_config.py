from __future__ import annotations

import json
from typing import Any, Dict

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from redis.asyncio import Redis

from ..models.app_setting import AppSetting


APP_CONFIG_DEFAULTS: Dict[str, Any] = {
    "context_max_turns": 100,
    "max_length": 0,
    "context_max_prompt_tokens": 32000,
    "interject_p": 5,
    "interject_cooldown": 60,
}


class AppConfigService:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession], redis: Redis):
        self._sessionmaker = sessionmaker
        self._redis = redis
        self._cache_key = "app:settings"

    async def get_all(self) -> Dict[str, Any]:
        cached = await self._redis.get(self._cache_key)
        if cached is not None:
            return json.loads(cached)

        async with self._sessionmaker() as session:
            res = await session.execute(select(AppSetting))
            data = {row.key: row.value for row in res.scalars()}

        merged = APP_CONFIG_DEFAULTS | data
        await self._redis.set(self._cache_key, json.dumps(merged, ensure_ascii=False), ex=300)
        return merged

    async def get(self, key: str) -> Any:
        values = await self.get_all()
        return values.get(key, APP_CONFIG_DEFAULTS.get(key))

    async def set(self, key: str, value: Any) -> None:
        async with self._sessionmaker() as session:
            obj = await session.get(AppSetting, key)
            if obj is None:
                obj = AppSetting(key=key, value=value)
                session.add(obj)
            else:
                obj.value = value
            await session.commit()
        await self._redis.delete(self._cache_key)
