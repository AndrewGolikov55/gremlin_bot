from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.services.spy.types import SpyChannelInfo, SpyPostPayload


@runtime_checkable
class ChannelReader(Protocol):
    async def resolve_channel(self, ref: str) -> SpyChannelInfo:
        """Resolve a public Telegram channel reference to normalized metadata."""
        ...

    async def fetch_latest_posts(self, username: str, *, limit: int) -> list[SpyPostPayload]:
        """Fetch the newest posts for a normalized public channel username."""
        ...
