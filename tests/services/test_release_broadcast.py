from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.chat import Chat, ChatSetting
from app.services.release_broadcast import ReleaseBroadcaster


async def _add_chat(
    sessionmaker: async_sessionmaker[AsyncSession],
    chat_id: int,
    *,
    is_active: bool = True,
    setting_active: bool | None = None,
) -> None:
    async with sessionmaker() as session:
        session.add(Chat(id=chat_id, title=f"chat {chat_id}", is_active=is_active))
        if setting_active is not None:
            session.add(ChatSetting(
                chat_id=chat_id,
                key="is_active",
                value=setting_active,
                updated_at=datetime.utcnow(),
            ))
        await session.commit()


def _make_broadcaster(sessionmaker: async_sessionmaker[AsyncSession]) -> ReleaseBroadcaster:
    app_config = AsyncMock()
    app_config.get = AsyncMock(return_value=None)
    return ReleaseBroadcaster(
        bot=AsyncMock(),
        sessionmaker=sessionmaker,
        app_config=app_config,
    )


@pytest.mark.asyncio
async def test_private_chats_excluded(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    await _add_chat(sessionmaker, chat_id=12345)       # positive = private
    await _add_chat(sessionmaker, chat_id=-100111222)  # negative = group

    broadcaster = _make_broadcaster(sessionmaker)
    ids = await broadcaster._active_chat_ids()

    assert 12345 not in ids
    assert -100111222 in ids


@pytest.mark.asyncio
async def test_chat_setting_disabled_excluded(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    await _add_chat(sessionmaker, chat_id=-100111111, setting_active=False)
    await _add_chat(sessionmaker, chat_id=-100222222, setting_active=True)
    await _add_chat(sessionmaker, chat_id=-100333333)  # no setting row → defaults True

    broadcaster = _make_broadcaster(sessionmaker)
    ids = await broadcaster._active_chat_ids()

    assert -100111111 not in ids
    assert -100222222 in ids
    assert -100333333 in ids


@pytest.mark.asyncio
async def test_chat_is_active_false_excluded(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    await _add_chat(sessionmaker, chat_id=-100999888, is_active=False)

    broadcaster = _make_broadcaster(sessionmaker)
    ids = await broadcaster._active_chat_ids()

    assert -100999888 not in ids
