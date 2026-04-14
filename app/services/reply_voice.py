"""Resolve transcript for a voice/video_note that the user is replying to.

Cache strategy: transcripts live in Message.text with a prefix marker
('[голосовое] ...' or '[круглое видео] ...'). On first transcription
we UPDATE the row; subsequent reply-chains get the cached text for free.
"""
from __future__ import annotations

import logging
from typing import Awaitable, Callable

from aiogram import Bot, types
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.message import Message
from .llm.whisper import TranscriptionResult, transcribe_file_id

logger = logging.getLogger(__name__)

VOICE_MARKER = "[голосовое]"
VIDEO_NOTE_MARKER = "[круглое видео]"

_TranscribeFn = Callable[..., Awaitable[TranscriptionResult | None]]


def _extract_cached_transcript(text: str | None) -> str | None:
    """Return the cached transcript suffix if `text` is a marker + transcript."""
    if not text:
        return None
    for marker in (VOICE_MARKER, VIDEO_NOTE_MARKER):
        if text.startswith(marker):
            suffix = text[len(marker):].strip()
            if suffix:
                return suffix
    return None


def _marker_for(text: str | None) -> str | None:
    if not text:
        return None
    for marker in (VOICE_MARKER, VIDEO_NOTE_MARKER):
        if text.startswith(marker):
            return marker
    return None


async def get_reply_voice_transcript(
    *,
    bot: Bot,
    message: types.Message,
    session: AsyncSession,
    max_seconds: int = 0,
    _transcribe: _TranscribeFn | None = None,
) -> str | None:
    """Return transcript of the voice/video_note that this message replies to.

    Priority:
    1. No reply_to_message -> None
    2. DB row exists with cached transcript -> return cached
    3. DB row exists with placeholder + tg_file_id -> transcribe, update DB, return
    4. No DB row but live update has voice/video_note -> transcribe, return (no DB write)
    5. Whisper fails on any path -> None

    The optional _transcribe arg is for testing.
    """
    reply = getattr(message, "reply_to_message", None)
    if reply is None:
        return None

    transcribe = _transcribe or transcribe_file_id
    chat_id = message.chat.id
    reply_message_id = getattr(reply, "message_id", None)

    cached_marker: str | None = None
    db_text: str | None = None
    db_file_id: str | None = None
    db_id: int | None = None

    if reply_message_id is not None:
        stmt = select(Message.id, Message.text, Message.tg_file_id).where(
            Message.chat_id == chat_id,
            Message.message_id == reply_message_id,
        )
        row = (await session.execute(stmt)).first()
        if row is not None:
            db_id, db_text, db_file_id = row
            # Step 2: cached transcript
            cached = _extract_cached_transcript(db_text)
            if cached is not None and db_file_id is not None:
                return cached
            cached_marker = _marker_for(db_text) if db_file_id else None

    file_id_to_transcribe: str | None = None
    duration_hint: float | None = None

    if cached_marker is not None and db_file_id is not None:
        # Step 3: placeholder + tg_file_id -> transcribe with DB update
        file_id_to_transcribe = db_file_id
    else:
        # Step 4: fall back to live update
        live_voice = getattr(reply, "voice", None)
        live_video_note = getattr(reply, "video_note", None)
        if live_voice is not None:
            file_id_to_transcribe = getattr(live_voice, "file_id", None)
            duration_hint = float(getattr(live_voice, "duration", 0) or 0)
        elif live_video_note is not None:
            file_id_to_transcribe = getattr(live_video_note, "file_id", None)
            duration_hint = float(getattr(live_video_note, "duration", 0) or 0)

    if not file_id_to_transcribe:
        return None

    result = await transcribe(
        bot,
        file_id_to_transcribe,
        max_seconds=max_seconds,
        duration_hint=duration_hint,
    )
    if result is None:
        return None

    # If we used a DB row, write back the transcript with the existing marker
    if db_id is not None and cached_marker is not None:
        new_text = f"{cached_marker} {result.text}"
        await session.execute(
            update(Message)
            .where(Message.id == db_id, Message.text == cached_marker)
            .values(text=new_text)
        )
        await session.commit()

    return result.text
