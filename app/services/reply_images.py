from __future__ import annotations

import logging
from typing import Awaitable, Callable

from aiogram import Bot, types
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.message import Message
from .llm.vision import download_file_id_as_data_url


logger = logging.getLogger(__name__)

MAX_REPLY_IMAGES = 10  # Telegram album cap

_DownloadFn = Callable[[Bot, str], Awaitable[str | None]]


_PHOTO_SIZE_CAP = 8 * 1024 * 1024


def _pick_reply_photo_size(photos: list[types.PhotoSize]) -> types.PhotoSize | None:
    if not photos:
        return None
    for photo in reversed(photos):
        size = getattr(photo, "file_size", None)
        if isinstance(size, int) and 0 < size <= _PHOTO_SIZE_CAP:
            return photo
    return photos[-1]


async def collect_reply_images(
    *,
    bot: Bot,
    message: types.Message,
    session: AsyncSession,
    _download: _DownloadFn | None = None,
) -> list[str]:
    """Collect up to MAX_REPLY_IMAGES data URLs for photos in the reply chain.

    Source priority:
      1. If the replied message is in the DB and has media_group_id: fetch all
         tg_file_id rows in that album (ordered by message_id), capped at MAX_REPLY_IMAGES.
      2. If the replied message is in the DB with just tg_file_id: one photo.
      3. Fallback: message.reply_to_message.photo from the aiogram update (one photo).
      4. Nothing found — empty list.

    Partial download failures are silently dropped (successful urls are still returned).
    The optional _download arg is for testing.
    """
    reply = getattr(message, "reply_to_message", None)
    if reply is None:
        return []

    download = _download or download_file_id_as_data_url
    chat_id = message.chat.id
    reply_message_id = getattr(reply, "message_id", None)

    file_ids: list[str] = []

    if reply_message_id is not None:
        stmt = select(Message.media_group_id, Message.tg_file_id).where(
            Message.chat_id == chat_id,
            Message.message_id == reply_message_id,
        )
        result = await session.execute(stmt)
        row = result.first()
        if row is not None:
            media_group_id, tg_file_id = row
            if media_group_id:
                album_stmt = (
                    select(Message.tg_file_id)
                    .where(
                        Message.chat_id == chat_id,
                        Message.media_group_id == media_group_id,
                        Message.tg_file_id.isnot(None),
                    )
                    .order_by(Message.message_id)
                    .limit(MAX_REPLY_IMAGES)
                )
                album_res = await session.execute(album_stmt)
                file_ids = [fid for (fid,) in album_res.all() if fid]
            elif tg_file_id:
                file_ids = [tg_file_id]

    if not file_ids:
        reply_photos = list(getattr(reply, "photo", None) or [])
        picked = _pick_reply_photo_size(reply_photos)
        if picked is not None:
            file_id_value = getattr(picked, "file_id", None)
            if isinstance(file_id_value, str) and file_id_value:
                file_ids = [file_id_value]

    if not file_ids:
        return []

    urls: list[str] = []
    for fid in file_ids[:MAX_REPLY_IMAGES]:
        url = await download(bot, fid)
        if url:
            urls.append(url)
    return urls
