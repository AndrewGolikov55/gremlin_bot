from __future__ import annotations

from datetime import datetime
from typing import Any, Dict

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from redis.asyncio import Redis

from ..models.chat import Chat, ChatSetting


DEFAULTS: Dict[str, Any] = {
    "is_active": True,
    "quiet_hours": None,  # e.g. "23:00-08:00"
    "style": "standup",  # standup|gopnik|boss|zoomer|jarvis
    "revive_enabled": True,
    "revive_after_hours": 48,
    "temperature": 1.0,
}


class SettingsService:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession], redis: Redis):
        self._sessionmaker = sessionmaker
        self._redis = redis

    async def get(self, chat_id: int, key: str) -> Any:
        cache_key = f"chat:{chat_id}:setting:{key}"
        cached = await self._redis.get(cache_key)
        if cached is not None:
            import json

            return json.loads(cached)
        async with self._sessionmaker() as session:
            res = await session.execute(
                select(ChatSetting).where(ChatSetting.chat_id == chat_id, ChatSetting.key == key)
            )
            row = res.scalar_one_or_none()
            value = row.value if row else DEFAULTS.get(key)
        await self._redis.set(cache_key, __serialize(value), ex=300)
        return value

    async def get_all(self, chat_id: int) -> Dict[str, Any]:
        out = DEFAULTS.copy()
        async with self._sessionmaker() as session:
            res = await session.execute(select(ChatSetting).where(ChatSetting.chat_id == chat_id))
            for s in res.scalars():
                out[s.key] = s.value
        return out

    async def set(self, chat_id: int, key: str, value: Any) -> None:
        async with self._sessionmaker() as session:
            # ensure chat exists
            chat = await session.get(Chat, chat_id)
            if chat is None:
                session.add(Chat(id=chat_id, title=str(chat_id), is_active=True))

            res = await session.execute(
                select(ChatSetting).where(ChatSetting.chat_id == chat_id, ChatSetting.key == key)
            )
            row = res.scalar_one_or_none()
            if row is None:
                row = ChatSetting(chat_id=chat_id, key=key, value=value, updated_at=datetime.utcnow())
                session.add(row)
            else:
                row.value = value
                row.updated_at = datetime.utcnow()
            await session.commit()
        # invalidate cache
        await self._redis.delete(f"chat:{chat_id}:setting:{key}")


def __serialize(value: Any) -> str:
    import json

    return json.dumps(value, ensure_ascii=False)
