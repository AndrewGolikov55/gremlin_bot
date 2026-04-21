from __future__ import annotations

from datetime import datetime
from typing import Any, cast
from unittest.mock import MagicMock

from aiogram import Bot
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.message import Message as DBMessage


def _build_interjector(sessionmaker: async_sessionmaker[AsyncSession]) -> Any:
    from app.services.interjector import InterjectorService
    svc = InterjectorService.__new__(InterjectorService)
    svc.bot = cast(Bot, MagicMock())
    svc.sessionmaker = sessionmaker
    svc.settings = cast(Any, MagicMock())
    svc.app_config = cast(Any, MagicMock())
    svc.context = cast(Any, MagicMock())
    svc.personas = cast(Any, MagicMock())
    svc.policy = cast(Any, MagicMock())
    svc.memory = cast(Any, MagicMock())
    svc.usage_limits = cast(Any, MagicMock())
    return svc


async def test_last_message_time_ignores_is_bot_filter(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """In a dead chat, a bot revive message must reset the revive clock.
    Otherwise revive keeps firing every cooldown period forever."""
    chat_id = -100
    human_msg_time = datetime(2026, 4, 19, 10, 38, 0)
    bot_revive_time = datetime(2026, 4, 21, 10, 38, 0)

    async with sessionmaker() as session:
        session.add(DBMessage(
            chat_id=chat_id, message_id=1, user_id=123,
            text="real human msg", is_bot=False, date=human_msg_time,
        ))
        session.add(DBMessage(
            chat_id=chat_id, message_id=2, user_id=999,
            text="bot revive msg", is_bot=True, date=bot_revive_time,
        ))
        await session.commit()

    svc = _build_interjector(sessionmaker)
    async with sessionmaker() as session:
        result = await svc._get_last_message_time(session, chat_id)

    assert result == bot_revive_time, (
        "Expected the latest message time regardless of is_bot; "
        "if this returns the human msg time, revive will keep spamming "
        "in dead chats every cooldown period."
    )
