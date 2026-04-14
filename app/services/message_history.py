from __future__ import annotations

from datetime import datetime, timezone

from aiogram import types
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..models.chat import Chat
from ..models.message import Message
from ..models.user import User


_PHOTO_SIZE_CAP_BYTES = 8 * 1024 * 1024


def _largest_storable_photo(photos: list) -> object | None:
    if not photos:
        return None
    for photo in reversed(photos):
        size = getattr(photo, "file_size", None)
        if isinstance(size, int) and 0 < size <= _PHOTO_SIZE_CAP_BYTES:
            return photo
    return photos[-1]


async def store_telegram_message(
    session: AsyncSession,
    message: types.Message,
    *,
    reply_to_message_id: int | None = None,
) -> bool:
    await _ensure_chat(session, message)
    await _upsert_user(session, message)
    return await _insert_message(session, message, reply_to_message_id=reply_to_message_id)


async def persist_telegram_message(
    sessionmaker: async_sessionmaker[AsyncSession],
    message: types.Message,
    *,
    reply_to_message_id: int | None = None,
) -> bool:
    async with sessionmaker() as session:
        created = await store_telegram_message(
            session,
            message,
            reply_to_message_id=reply_to_message_id,
        )
        await session.commit()
        return created


async def _ensure_chat(session: AsyncSession, message: types.Message) -> None:
    chat = await session.get(Chat, message.chat.id)
    if chat is None:
        chat = Chat(id=message.chat.id, title=message.chat.title or str(message.chat.id), is_active=True)
        session.add(chat)
    elif message.chat.title and chat.title != message.chat.title:
        chat.title = message.chat.title


async def _upsert_user(session: AsyncSession, message: types.Message) -> None:
    if not message.from_user:
        return

    stmt = select(User).where(User.tg_id == message.from_user.id)
    res = await session.execute(stmt)
    user = res.scalar_one_or_none()

    username = message.from_user.username or message.from_user.full_name
    if user is None:
        user = User(tg_id=message.from_user.id, username=username, is_admin_cached=False)
        session.add(user)
    elif username and user.username != username:
        user.username = username


async def _insert_message(
    session: AsyncSession,
    message: types.Message,
    *,
    reply_to_message_id: int | None = None,
) -> bool:
    stmt = select(Message.id).where(
        Message.chat_id == message.chat.id,
        Message.message_id == message.message_id,
    )
    res = await session.execute(stmt)
    if res.scalar_one_or_none() is not None:
        return False

    msg_date = message.date or datetime.utcnow()
    if msg_date.tzinfo is not None:
        msg_date = msg_date.astimezone(timezone.utc).replace(tzinfo=None)

    tg_file_id: str | None = None
    photo_sizes = list(message.photo or [])
    if photo_sizes:
        picked = _largest_storable_photo(photo_sizes)
        if picked is not None:
            file_id_value = getattr(picked, "file_id", None)
            if isinstance(file_id_value, str) and file_id_value:
                tg_file_id = file_id_value

    if tg_file_id is None:
        voice = getattr(message, "voice", None)
        if voice is not None:
            file_id_value = getattr(voice, "file_id", None)
            if isinstance(file_id_value, str) and file_id_value:
                tg_file_id = file_id_value

    if tg_file_id is None:
        video_note = getattr(message, "video_note", None)
        if video_note is not None:
            file_id_value = getattr(video_note, "file_id", None)
            if isinstance(file_id_value, str) and file_id_value:
                tg_file_id = file_id_value

    msg = Message(
        chat_id=message.chat.id,
        message_id=message.message_id,
        user_id=message.from_user.id if message.from_user else 0,
        text=render_message_storage_text(message),
        reply_to_id=reply_to_message_id
        if reply_to_message_id is not None
        else message.reply_to_message.message_id
        if message.reply_to_message
        else None,
        date=msg_date,
        is_bot=bool(message.from_user and message.from_user.is_bot),
        tg_file_id=tg_file_id,
        media_group_id=getattr(message, "media_group_id", None),
    )
    session.add(msg)
    return True


def render_message_storage_text(message: types.Message) -> str:
    if message.text:
        return message.text
    if getattr(message, "voice", None) is not None:
        return "[голосовое]"
    if getattr(message, "video_note", None) is not None:
        return "[круглое видео]"
    if message.photo:
        caption = (message.caption or "").strip()
        return f"[photo] {caption}" if caption else "[photo]"
    if message.sticker:
        return "[sticker]"
    if message.animation:
        caption = (message.caption or "").strip()
        return f"[animation] {caption}" if caption else "[animation]"
    if message.video:
        caption = (message.caption or "").strip()
        return f"[video] {caption}" if caption else "[video]"
    if message.document:
        caption = (message.caption or "").strip()
        return f"[document] {caption}" if caption else "[document]"
    return message.caption or ""
