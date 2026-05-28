from __future__ import annotations

from datetime import datetime

from app.services.spy.types import SpyChannelInfo, SpyMedia, SpyPostPayload


def test_spy_post_payload_stores_post_metadata() -> None:
    payload = SpyPostPayload(
        external_post_id="123",
        text="hello",
        published_at=datetime(2026, 5, 27, 12, 0, 0),
        source_url="https://t.me/channel/123",
        media=[],
        raw={"id": 123},
    )
    assert payload.external_post_id == "123"
    assert payload.media == []


def test_spy_media_image_marker() -> None:
    media = SpyMedia(kind="photo", file_id="abc", data_url="data:image/jpeg;base64,x", raw={})
    assert media.is_image is True


def test_spy_media_image_marker_from_mime_type() -> None:
    media = SpyMedia(kind="document", mime_type="image/png")
    assert media.is_image is True


def test_spy_channel_info() -> None:
    info = SpyChannelInfo(
        username="channel",
        title="Channel",
        telegram_channel_id=123,
        access_mode="mtproto",
    )
    assert info.public_url == "https://t.me/channel"
