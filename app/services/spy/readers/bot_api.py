from __future__ import annotations

from typing import Any

from app.services.spy.types import SpyChannelInfo, SpyMedia, SpyPostPayload


class BotApiChannelReader:
    """Mapper for Bot API channel_post updates.

    Bot API ingestion is push-based: Telegram sends channel posts to the bot
    when the bot is present in a channel. This class intentionally does not
    poll history; it only maps received channel_post messages into the shared
    spy domain payloads.
    """

    def channel_info_from_channel_post(self, message: Any) -> SpyChannelInfo:
        chat = getattr(message, "chat")
        username = (getattr(chat, "username", None) or "").lower()
        return SpyChannelInfo(
            username=username,
            title=getattr(chat, "title", None),
            telegram_channel_id=getattr(chat, "id", None),
            access_mode="bot_api",
        )

    def payload_from_channel_post(self, message: Any) -> SpyPostPayload:
        chat = getattr(message, "chat")
        message_id = getattr(message, "message_id")
        username = (getattr(chat, "username", None) or "").lower()
        source_url = f"https://t.me/{username}/{message_id}" if username else None
        media_group_id = getattr(message, "media_group_id", None)
        return SpyPostPayload(
            external_post_id=str(message_id),
            text=getattr(message, "text", None) or getattr(message, "caption", None) or None,
            published_at=getattr(message, "date", None),
            source_url=source_url,
            media=self._extract_media(message),
            raw={"message_id": message_id, "chat_id": getattr(chat, "id", None)},
            media_group_id=str(media_group_id) if media_group_id is not None else None,
        )

    def _extract_media(self, message: Any) -> list[SpyMedia]:
        if getattr(message, "photo", None):
            photo = self._pick_largest_photo(getattr(message, "photo"))
            return [
                SpyMedia(
                    kind="photo",
                    file_id=getattr(photo, "file_id", None),
                    width=getattr(photo, "width", None),
                    height=getattr(photo, "height", None),
                    raw={"file_size": getattr(photo, "file_size", None)},
                )
            ]
        document = getattr(message, "document", None)
        if document is not None:
            return [
                SpyMedia(
                    kind="document",
                    file_id=getattr(document, "file_id", None),
                    mime_type=getattr(document, "mime_type", None),
                    raw={"file_name": getattr(document, "file_name", None)},
                )
            ]
        return []

    def _pick_largest_photo(self, photos: list[Any]) -> Any:
        return max(
            photos,
            key=lambda photo: (getattr(photo, "width", 0) or 0) * (getattr(photo, "height", 0) or 0),
        )
