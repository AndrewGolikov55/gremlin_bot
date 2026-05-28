from __future__ import annotations

from datetime import datetime
from typing import assert_type

from app.services.spy.readers.base import ChannelReader
from app.services.spy.types import SpyChannelInfo, SpyPostPayload


class InMemoryChannelReader:
    async def resolve_channel(self, ref: str) -> SpyChannelInfo:
        return SpyChannelInfo(username=ref, title="Test Channel")

    async def fetch_latest_posts(self, username: str, *, limit: int) -> list[SpyPostPayload]:
        return [
            SpyPostPayload(
                external_post_id="1",
                text=f"latest from {username}",
                published_at=datetime(2026, 5, 27, 12, 0, 0),
                source_url=f"https://t.me/{username}/1",
            )
        ][:limit]


def test_channel_reader_protocol_accepts_structural_implementation() -> None:
    reader: ChannelReader = InMemoryChannelReader()

    assert isinstance(reader, ChannelReader)
    assert_type(reader, ChannelReader)
