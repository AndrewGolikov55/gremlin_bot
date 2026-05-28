from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from app.services.spy.readers.bot_api import BotApiChannelReader


@dataclass(slots=True)
class FakeChat:
    id: int
    username: str | None
    title: str | None


@dataclass(slots=True)
class FakePhotoSize:
    file_id: str
    width: int
    height: int
    file_size: int | None = None


@dataclass(slots=True)
class FakeDocument:
    file_id: str
    mime_type: str | None = None
    file_name: str | None = None


@dataclass(slots=True)
class FakeMessage:
    message_id: int
    chat: FakeChat
    date: datetime | None
    text: str | None = None
    caption: str | None = None
    photo: list[FakePhotoSize] | None = None
    document: FakeDocument | None = None
    media_group_id: str | None = None


def test_channel_info_from_channel_post_maps_public_channel() -> None:
    reader = BotApiChannelReader()
    message = FakeMessage(
        message_id=101,
        chat=FakeChat(id=-100777, username="SomeChannel", title="Some Channel"),
        date=datetime(2026, 5, 27, 12, 0, tzinfo=timezone.utc),
    )

    info = reader.channel_info_from_channel_post(message)

    assert info.username == "somechannel"
    assert info.title == "Some Channel"
    assert info.telegram_channel_id == -100777
    assert info.access_mode == "bot_api"
    assert info.public_url == "https://t.me/somechannel"


def test_payload_from_channel_post_maps_text_and_permalink() -> None:
    reader = BotApiChannelReader()
    message = FakeMessage(
        message_id=102,
        chat=FakeChat(id=-100777, username="SomeChannel", title="Some Channel"),
        date=datetime(2026, 5, 27, 12, 5, tzinfo=timezone.utc),
        text="hello from channel",
    )

    payload = reader.payload_from_channel_post(message)

    assert payload.external_post_id == "102"
    assert payload.text == "hello from channel"
    assert payload.published_at == datetime(2026, 5, 27, 12, 5, tzinfo=timezone.utc)
    assert payload.source_url == "https://t.me/somechannel/102"
    assert payload.media == []
    assert payload.raw == {"message_id": 102, "chat_id": -100777}


def test_payload_from_channel_post_maps_photo_caption_and_media_group() -> None:
    reader = BotApiChannelReader()
    message = FakeMessage(
        message_id=103,
        chat=FakeChat(id=-100777, username="SomeChannel", title="Some Channel"),
        date=datetime(2026, 5, 27, 12, 10, tzinfo=timezone.utc),
        caption="look at this",
        photo=[
            FakePhotoSize(file_id="small", width=320, height=200, file_size=10_000),
            FakePhotoSize(file_id="large", width=1280, height=800, file_size=100_000),
        ],
        media_group_id="album-1",
    )

    payload = reader.payload_from_channel_post(message)

    assert payload.text == "look at this"
    assert payload.media_group_id == "album-1"
    assert len(payload.media) == 1
    media = payload.media[0]
    assert media.kind == "photo"
    assert media.file_id == "large"
    assert media.width == 1280
    assert media.height == 800
    assert media.is_image is True


def test_payload_from_channel_post_maps_document_image() -> None:
    reader = BotApiChannelReader()
    message = FakeMessage(
        message_id=104,
        chat=FakeChat(id=-100777, username="SomeChannel", title="Some Channel"),
        date=None,
        document=FakeDocument(file_id="doc-file", mime_type="image/png", file_name="pic.png"),
    )

    payload = reader.payload_from_channel_post(message)

    assert payload.text is None
    assert len(payload.media) == 1
    media = payload.media[0]
    assert media.kind == "document"
    assert media.file_id == "doc-file"
    assert media.mime_type == "image/png"
    assert media.is_image is True
    assert media.raw == {"file_name": "pic.png"}


def test_payload_from_private_or_anonymous_channel_has_no_public_url() -> None:
    reader = BotApiChannelReader()
    message = FakeMessage(
        message_id=105,
        chat=FakeChat(id=-100777, username=None, title="Private Channel"),
        date=None,
        text="private",
    )

    info = reader.channel_info_from_channel_post(message)
    payload = reader.payload_from_channel_post(message)

    assert info.username == ""
    assert info.public_url is None
    assert payload.source_url is None
