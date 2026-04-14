from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.message import Message
from app.services.reply_voice import get_reply_voice_transcript


def _msg_with_reply(
    chat_id: int = 100,
    reply_to_id: int | None = 50,
    reply_voice: SimpleNamespace | None = None,
    reply_video_note: SimpleNamespace | None = None,
) -> SimpleNamespace:
    reply_to = None
    if reply_to_id is not None:
        reply_to = SimpleNamespace(
            message_id=reply_to_id,
            voice=reply_voice,
            video_note=reply_video_note,
        )
    return SimpleNamespace(
        chat=SimpleNamespace(id=chat_id),
        reply_to_message=reply_to,
    )


async def _seed(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    chat_id: int,
    message_id: int,
    text: str,
    tg_file_id: str | None,
) -> None:
    async with sessionmaker() as session:
        session.add(Message(
            chat_id=chat_id,
            message_id=message_id,
            user_id=1,
            text=text,
            reply_to_id=None,
            date=datetime.utcnow(),
            is_bot=False,
            tg_file_id=tg_file_id,
            media_group_id=None,
        ))
        await session.commit()


@pytest.mark.asyncio
async def test_returns_none_when_no_reply(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    msg = SimpleNamespace(chat=SimpleNamespace(id=100), reply_to_message=None)
    async with sessionmaker() as session:
        result = await get_reply_voice_transcript(
            bot=AsyncMock(),
            message=msg,  # type: ignore[arg-type]
            session=session,
        )
    assert result is None


@pytest.mark.asyncio
async def test_returns_cached_transcript_from_db(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    await _seed(
        sessionmaker,
        chat_id=100,
        message_id=50,
        text="[голосовое] привет, как дела",
        tg_file_id="voice-fid",
    )
    msg = _msg_with_reply()
    transcribe = AsyncMock()  # should NOT be called
    async with sessionmaker() as session:
        result = await get_reply_voice_transcript(
            bot=AsyncMock(),
            message=msg,  # type: ignore[arg-type]
            session=session,
            _transcribe=transcribe,
        )
    assert result == "привет, как дела"
    transcribe.assert_not_called()


@pytest.mark.asyncio
async def test_transcribes_when_only_placeholder_in_db(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    await _seed(
        sessionmaker,
        chat_id=100,
        message_id=50,
        text="[голосовое]",
        tg_file_id="voice-fid",
    )
    msg = _msg_with_reply()
    from app.services.llm.whisper import TranscriptionResult
    transcribe = AsyncMock(return_value=TranscriptionResult(text="свежая транскрипция", duration_seconds=8.0))

    async with sessionmaker() as session:
        result = await get_reply_voice_transcript(
            bot=AsyncMock(),
            message=msg,  # type: ignore[arg-type]
            session=session,
            _transcribe=transcribe,
        )

    assert result == "свежая транскрипция"
    transcribe.assert_awaited_once()

    # Verify DB was updated with transcript
    from sqlalchemy import select
    async with sessionmaker() as session:
        row = (await session.execute(select(Message.text).where(Message.message_id == 50))).scalar_one()
    assert row == "[голосовое] свежая транскрипция"


@pytest.mark.asyncio
async def test_fallback_to_live_voice_when_not_in_db(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    live_voice = SimpleNamespace(file_id="live-voice-fid", duration=5.0)
    msg = _msg_with_reply(reply_to_id=999, reply_voice=live_voice)
    from app.services.llm.whisper import TranscriptionResult
    transcribe = AsyncMock(return_value=TranscriptionResult(text="из live-update", duration_seconds=5.0))

    async with sessionmaker() as session:
        result = await get_reply_voice_transcript(
            bot=AsyncMock(),
            message=msg,  # type: ignore[arg-type]
            session=session,
            _transcribe=transcribe,
        )

    assert result == "из live-update"
    transcribe.assert_awaited_once()


@pytest.mark.asyncio
async def test_returns_none_when_whisper_fails(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    await _seed(
        sessionmaker,
        chat_id=100,
        message_id=50,
        text="[голосовое]",
        tg_file_id="voice-fid",
    )
    msg = _msg_with_reply()
    transcribe = AsyncMock(return_value=None)

    async with sessionmaker() as session:
        result = await get_reply_voice_transcript(
            bot=AsyncMock(),
            message=msg,  # type: ignore[arg-type]
            session=session,
            _transcribe=transcribe,
        )

    assert result is None
