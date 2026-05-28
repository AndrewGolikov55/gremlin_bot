from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True, slots=True)
class SpyMedia:
    kind: str
    file_id: str | None = None
    data_url: str | None = None
    mime_type: str | None = None
    width: int | None = None
    height: int | None = None
    raw: dict[str, object] = field(default_factory=dict)

    @property
    def is_image(self) -> bool:
        return self.kind == "photo" or bool(self.mime_type and self.mime_type.startswith("image/"))


@dataclass(frozen=True, slots=True)
class SpyChannelInfo:
    username: str
    title: str | None = None
    telegram_channel_id: int | None = None
    access_mode: str = "mtproto"

    @property
    def public_url(self) -> str | None:
        if not self.username:
            return None
        return f"https://t.me/{self.username}"


@dataclass(frozen=True, slots=True)
class SpyPostPayload:
    external_post_id: str
    text: str | None
    published_at: datetime | None = None
    source_url: str | None = None
    media: list[SpyMedia] = field(default_factory=list)
    raw: dict[str, object] = field(default_factory=dict)
    media_group_id: str | None = None
