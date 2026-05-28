from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramMigrateToChat
from aiogram.methods import SendMessage
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.chat import Chat, ChatSetting
from app.services import release_broadcast
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


@pytest.mark.asyncio
async def test_broadcast_retries_migrated_group_once(
    sessionmaker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_chat_id = -646530977
    migrated_chat_id = -100646530977
    await _add_chat(sessionmaker, chat_id=old_chat_id)

    monkeypatch.setattr(release_broadcast, "get_version", lambda: "2026.05.28.1")
    monkeypatch.setattr(release_broadcast, "read_release_notes", lambda: "notes")

    bot = AsyncMock()
    bot.send_message.side_effect = [
        TelegramMigrateToChat(
            method=SendMessage(chat_id=old_chat_id, text="notes"),
            message="group chat was upgraded",
            migrate_to_chat_id=migrated_chat_id,
        ),
        AsyncMock(),
    ]
    app_config = AsyncMock()
    app_config.get = AsyncMock(return_value="2026.05.28.0")

    broadcaster = ReleaseBroadcaster(
        bot=bot,
        sessionmaker=sessionmaker,
        app_config=app_config,
    )

    await broadcaster.broadcast_if_new_version()

    assert bot.send_message.call_count == 2
    bot.send_message.assert_any_await(old_chat_id, "notes", parse_mode=None)
    bot.send_message.assert_any_await(migrated_chat_id, "notes", parse_mode=None)
    app_config.set.assert_awaited_once_with("last_broadcasted_version", "2026.05.28.1")
