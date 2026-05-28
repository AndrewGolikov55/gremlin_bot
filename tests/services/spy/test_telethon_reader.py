from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import pytest

from app.services.spy.readers.telethon import TelethonChannelReader


@dataclass(slots=True)
class FakeEntity:
    id: int
    username: str | None
    title: str | None


@dataclass(slots=True)
class FakePhotoSize:
    w: int
    h: int


@dataclass(slots=True)
class FakePhoto:
    id: int
    sizes: list[FakePhotoSize]


@dataclass(slots=True)
class FakeMessageMediaPhoto:
    photo: FakePhoto


@dataclass(slots=True)
class FakeDocumentAttributeImageSize:
    w: int
    h: int


@dataclass(slots=True)
class FakeDocument:
    id: int
    mime_type: str | None
    attributes: list[object]


@dataclass(slots=True)
class FakeMessageMediaDocument:
    document: FakeDocument


@dataclass(slots=True)
class FakeMessage:
    id: int
    message: str | None
    date: datetime | None
    media: object | None = None
    grouped_id: int | None = None


class FakeClient:
    def __init__(self) -> None:
        self.resolved_refs: list[str] = []
        self.fetches: list[tuple[str, int]] = []

    async def get_entity(self, ref: str) -> FakeEntity:
        self.resolved_refs.append(ref)
        return FakeEntity(id=777, username="SomeChannel", title="Some Channel")

    async def get_messages(self, username: str, *, limit: int) -> list[FakeMessage]:
        self.fetches.append((username, limit))
        return [
            FakeMessage(
                id=10,
                message="photo post",
                date=datetime(2026, 5, 27, 12, 0, tzinfo=timezone.utc),
                media=FakeMessageMediaPhoto(
                    photo=FakePhoto(id=55, sizes=[FakePhotoSize(w=320, h=200), FakePhotoSize(w=1280, h=800)])
                ),
                grouped_id=999,
            ),
            FakeMessage(
                id=9,
                message=None,
                date=None,
                media=FakeMessageMediaDocument(
                    document=FakeDocument(
                        id=44,
                        mime_type="image/png",
                        attributes=[FakeDocumentAttributeImageSize(w=640, h=480)],
                    )
                ),
            ),
        ]


@pytest.mark.asyncio
async def test_resolve_channel_normalizes_ref_and_maps_entity() -> None:
    client = FakeClient()
    reader = TelethonChannelReader(client)

    info = await reader.resolve_channel("https://t.me/SomeChannel/123")

    assert client.resolved_refs == ["somechannel"]
    assert info.username == "somechannel"
    assert info.title == "Some Channel"
    assert info.telegram_channel_id == 777
    assert info.access_mode == "mtproto"
    assert info.public_url == "https://t.me/somechannel"


@pytest.mark.asyncio
async def test_fetch_latest_posts_maps_messages_and_media() -> None:
    client = FakeClient()
    reader = TelethonChannelReader(client)

    posts = await reader.fetch_latest_posts("somechannel", limit=2)

    assert client.fetches == [("somechannel", 2)]
    assert [post.external_post_id for post in posts] == ["10", "9"]
    assert posts[0].text == "photo post"
    assert posts[0].published_at == datetime(2026, 5, 27, 12, 0, tzinfo=timezone.utc)
    assert posts[0].source_url == "https://t.me/somechannel/10"
    assert posts[0].media_group_id == "999"
    assert len(posts[0].media) == 1
    assert posts[0].media[0].kind == "photo"
    assert posts[0].media[0].file_id == "55"
    assert posts[0].media[0].width == 1280
    assert posts[0].media[0].height == 800

    assert posts[1].text is None
    assert posts[1].source_url == "https://t.me/somechannel/9"
    assert posts[1].media[0].kind == "document"
    assert posts[1].media[0].mime_type == "image/png"
    assert posts[1].media[0].is_image is True
    assert posts[1].media[0].width == 640
    assert posts[1].media[0].height == 480
