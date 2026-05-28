from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Protocol

from app.services.spy.refs import normalize_channel_ref
from app.services.spy.types import SpyChannelInfo, SpyMedia, SpyPostPayload


class _TelethonClient(Protocol):
    async def get_entity(self, ref: str) -> Any: ...

    async def get_messages(self, username: str, *, limit: int) -> Iterable[Any]: ...


class TelethonChannelReader:
    def __init__(self, client: _TelethonClient) -> None:
        self._client = client

    async def resolve_channel(self, ref: str) -> SpyChannelInfo:
        username = normalize_channel_ref(ref)
        entity = await self._client.get_entity(username)
        entity_username = getattr(entity, "username", None) or username
        return SpyChannelInfo(
            username=str(entity_username).lower(),
            title=getattr(entity, "title", None),
            telegram_channel_id=getattr(entity, "id", None),
            access_mode="mtproto",
        )

    async def fetch_latest_posts(self, username: str, *, limit: int) -> list[SpyPostPayload]:
        messages = await self._client.get_messages(username, limit=limit)
        return [self._message_to_payload(username, message) for message in messages]

    def _message_to_payload(self, username: str, message: Any) -> SpyPostPayload:
        message_id = getattr(message, "id")
        media = self._extract_media(message)
        grouped_id = getattr(message, "grouped_id", None)
        return SpyPostPayload(
            external_post_id=str(message_id),
            text=getattr(message, "message", None) or None,
            published_at=getattr(message, "date", None),
            source_url=f"https://t.me/{username}/{message_id}",
            media=media,
            raw={"id": message_id},
            media_group_id=str(grouped_id) if grouped_id is not None else None,
        )

    def _extract_media(self, message: Any) -> list[SpyMedia]:
        media = getattr(message, "media", None)
        if media is None:
            return []
        photo = getattr(message, "photo", None) or getattr(media, "photo", None)
        if photo is not None:
            width, height = self._largest_photo_dimensions(photo)
            return [
                SpyMedia(
                    kind="photo",
                    file_id=str(getattr(photo, "id", "")) or None,
                    width=width,
                    height=height,
                    raw={"id": getattr(photo, "id", None)},
                )
            ]
        document = getattr(message, "document", None) or getattr(media, "document", None)
        if document is not None:
            width, height = self._document_dimensions(document)
            return [
                SpyMedia(
                    kind="document",
                    file_id=str(getattr(document, "id", "")) or None,
                    mime_type=getattr(document, "mime_type", None),
                    width=width,
                    height=height,
                    raw={"id": getattr(document, "id", None)},
                )
            ]
        if self._is_photo(media):
            width, height = self._largest_photo_dimensions(media)
            return [
                SpyMedia(
                    kind="photo",
                    file_id=str(getattr(media, "id", "")) or None,
                    width=width,
                    height=height,
                    raw={"id": getattr(media, "id", None)},
                )
            ]
        if self._is_document(media):
            width, height = self._document_dimensions(media)
            return [
                SpyMedia(
                    kind="document",
                    file_id=str(getattr(media, "id", "")) or None,
                    mime_type=getattr(media, "mime_type", None),
                    width=width,
                    height=height,
                    raw={"id": getattr(media, "id", None)},
                )
            ]
        return [SpyMedia(kind=type(media).__name__, raw={"type": type(media).__name__})]

    def _is_photo(self, media: Any) -> bool:
        return hasattr(media, "sizes")

    def _is_document(self, media: Any) -> bool:
        return hasattr(media, "mime_type") or hasattr(media, "attributes")

    def _largest_photo_dimensions(self, media: Any) -> tuple[int | None, int | None]:
        sizes = getattr(media, "sizes", None) or []
        best = max(sizes, key=lambda size: (getattr(size, "w", 0) or 0) * (getattr(size, "h", 0) or 0), default=None)
        if best is None:
            return None, None
        return getattr(best, "w", None), getattr(best, "h", None)

    def _document_dimensions(self, media: Any) -> tuple[int | None, int | None]:
        for attribute in getattr(media, "attributes", None) or []:
            width = getattr(attribute, "w", None)
            height = getattr(attribute, "h", None)
            if width is not None or height is not None:
                return width, height
        return None, None
