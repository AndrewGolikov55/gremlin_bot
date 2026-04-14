from __future__ import annotations

import base64
import io
import logging
import mimetypes

from aiogram import Bot


logger = logging.getLogger(__name__)


async def download_file_id_as_data_url(
    bot: Bot,
    file_id: str,
    *,
    max_bytes: int = 8 * 1024 * 1024,
) -> str | None:
    """Download a Telegram file by file_id and return a data: URL.

    Returns None if the file cannot be fetched (missing file_path, empty payload).
    Oversized files (> max_bytes) are still returned — caller decides whether to drop.
    """
    try:
        telegram_file = await bot.get_file(file_id)
    except Exception as exc:
        logger.warning("Failed to resolve file_id=%s: %s", file_id, exc)
        return None

    file_path = getattr(telegram_file, "file_path", None)
    if not file_path:
        logger.warning("Empty file_path for file_id=%s", file_id)
        return None

    buffer = io.BytesIO()
    try:
        await bot.download_file(file_path, destination=buffer)
    except Exception as exc:
        logger.warning("Failed to download file_id=%s path=%s: %s", file_id, file_path, exc)
        return None

    payload = buffer.getvalue()
    if not payload:
        logger.warning("Downloaded empty payload for file_id=%s path=%s", file_id, file_path)
        return None

    if len(payload) > max_bytes:
        logger.warning(
            "Downloaded oversized payload (%d bytes) for file_id=%s; returning anyway",
            len(payload),
            file_id,
        )

    mime_type, _encoding = mimetypes.guess_type(file_path)
    if not mime_type:
        mime_type = "image/jpeg"
    encoded = base64.b64encode(payload).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"
