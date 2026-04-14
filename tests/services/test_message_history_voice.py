from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.services.message_history import store_telegram_message


def _fake_chat(chat_id: int = 700) -> SimpleNamespace:
    return SimpleNamespace(id=chat_id, type="supergroup", title="Test", username=None)


def _fake_user(user_id: int = 42) -> SimpleNamespace:
    return SimpleNamespace(
        id=user_id,
        is_bot=False,
        username="alice",
        full_name="Alice",
        first_name="Alice",
        last_name=None,
    )


def _fake_message(
    *,
    message_id: int,
    voice: SimpleNamespace | None = None,
    video_note: SimpleNamespace | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        message_id=message_id,
        chat=_fake_chat(),
        from_user=_fake_user(),
        date=datetime.utcnow(),
        text=None,
        caption=None,
        photo=None,
        sticker=None,
        animation=None,
        video=None,
        document=None,
        voice=voice,
        video_note=video_note,
        audio=None,
        reply_to_message=None,
        media_group_id=None,
    )


@pytest.mark.asyncio
async def test_voice_message_persists_file_id_and_placeholder(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    voice = SimpleNamespace(
        file_id="voice-fid",
        file_unique_id="u1",
        duration=12,
        mime_type="audio/ogg",
        file_size=20_000,
    )
    msg = _fake_message(message_id=200, voice=voice)

    async with sessionmaker() as session:
        await store_telegram_message(session, msg)  # type: ignore[arg-type]
        await session.commit()

    from sqlalchemy import select

    from app.models.message import Message

    async with sessionmaker() as session:
        result = await session.execute(select(Message).where(Message.message_id == 200))
        saved = result.scalar_one()

    assert saved.tg_file_id == "voice-fid"
    assert saved.text == "[голосовое]"
    assert saved.media_group_id is None


@pytest.mark.asyncio
async def test_video_note_persists_file_id_and_placeholder(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    video_note = SimpleNamespace(
        file_id="vnote-fid",
        file_unique_id="u2",
        duration=8,
        length=240,
        file_size=50_000,
    )
    msg = _fake_message(message_id=201, video_note=video_note)

    async with sessionmaker() as session:
        await store_telegram_message(session, msg)  # type: ignore[arg-type]
        await session.commit()

    from sqlalchemy import select

    from app.models.message import Message

    async with sessionmaker() as session:
        result = await session.execute(select(Message).where(Message.message_id == 201))
        saved = result.scalar_one()

    assert saved.tg_file_id == "vnote-fid"
    assert saved.text == "[круглое видео]"
