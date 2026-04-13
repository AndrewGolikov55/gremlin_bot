from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest

from app.services.message_history import store_telegram_message


def _fake_chat(chat_id: int = 500):
    return SimpleNamespace(id=chat_id, type="supergroup", title="Test", username=None)


def _fake_user(user_id: int = 42):
    return SimpleNamespace(
        id=user_id,
        is_bot=False,
        username="alice",
        full_name="Alice",
        first_name="Alice",
        last_name=None,
    )


def _fake_photo(file_id: str, file_size: int = 100_000):
    return SimpleNamespace(file_id=file_id, file_unique_id="u_" + file_id, file_size=file_size, width=800, height=600)


def _fake_message(
    *,
    message_id: int,
    text: str | None = None,
    photo: list | None = None,
    caption: str | None = None,
    media_group_id: str | None = None,
):
    return SimpleNamespace(
        message_id=message_id,
        chat=_fake_chat(),
        from_user=_fake_user(),
        date=datetime.utcnow(),
        text=text,
        caption=caption,
        photo=photo,
        sticker=None,
        animation=None,
        video=None,
        document=None,
        reply_to_message=None,
        media_group_id=media_group_id,
    )


@pytest.mark.asyncio
async def test_photo_message_persists_file_id(sessionmaker) -> None:
    photos = [_fake_photo("small", 2_000), _fake_photo("large", 500_000)]
    msg = _fake_message(message_id=10, photo=photos, caption="hi", media_group_id=None)

    async with sessionmaker() as session:
        await store_telegram_message(session, msg)
        await session.commit()

    from sqlalchemy import select
    from app.models.message import Message

    async with sessionmaker() as session:
        result = await session.execute(select(Message).where(Message.message_id == 10))
        saved = result.scalar_one()

    assert saved.tg_file_id == "large"
    assert saved.media_group_id is None


@pytest.mark.asyncio
async def test_album_message_persists_media_group_id(sessionmaker) -> None:
    photos = [_fake_photo("f1", 10_000)]
    msg = _fake_message(message_id=11, photo=photos, media_group_id="gid-xyz")

    async with sessionmaker() as session:
        await store_telegram_message(session, msg)
        await session.commit()

    from sqlalchemy import select
    from app.models.message import Message

    async with sessionmaker() as session:
        result = await session.execute(select(Message).where(Message.message_id == 11))
        saved = result.scalar_one()

    assert saved.tg_file_id == "f1"
    assert saved.media_group_id == "gid-xyz"


@pytest.mark.asyncio
async def test_text_message_leaves_photo_refs_null(sessionmaker) -> None:
    msg = _fake_message(message_id=12, text="hello")

    async with sessionmaker() as session:
        await store_telegram_message(session, msg)
        await session.commit()

    from sqlalchemy import select
    from app.models.message import Message

    async with sessionmaker() as session:
        result = await session.execute(select(Message).where(Message.message_id == 12))
        saved = result.scalar_one()

    assert saved.tg_file_id is None
    assert saved.media_group_id is None


@pytest.mark.asyncio
async def test_oversized_photo_falls_back_to_last(sessionmaker) -> None:
    huge = 16 * 1024 * 1024
    photos = [_fake_photo("only", huge)]
    msg = _fake_message(message_id=13, photo=photos)

    async with sessionmaker() as session:
        await store_telegram_message(session, msg)
        await session.commit()

    from sqlalchemy import select
    from app.models.message import Message

    async with sessionmaker() as session:
        result = await session.execute(select(Message).where(Message.message_id == 13))
        saved = result.scalar_one()

    # No size within cap — we still save the only available file_id so replies can try it.
    assert saved.tg_file_id == "only"
