from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.models.message import Message
from app.services.reply_images import collect_reply_images


async def _seed_photo_message(
    sessionmaker,
    *,
    chat_id: int,
    message_id: int,
    tg_file_id: str,
    media_group_id: str | None = None,
) -> None:
    async with sessionmaker() as session:
        session.add(Message(
            chat_id=chat_id,
            message_id=message_id,
            user_id=1,
            text="[photo]",
            reply_to_id=None,
            date=datetime.utcnow(),
            is_bot=False,
            tg_file_id=tg_file_id,
            media_group_id=media_group_id,
        ))
        await session.commit()


@pytest.mark.asyncio
async def test_collect_images_from_db_album(sessionmaker) -> None:
    chat_id = 777
    for mid, fid in [(10, "a"), (11, "b"), (12, "c")]:
        await _seed_photo_message(
            sessionmaker,
            chat_id=chat_id,
            message_id=mid,
            tg_file_id=fid,
            media_group_id="album-xyz",
        )

    incoming = SimpleNamespace(
        chat=SimpleNamespace(id=chat_id),
        reply_to_message=SimpleNamespace(message_id=10, photo=None),
    )

    download = AsyncMock(side_effect=lambda _bot, fid: f"url-{fid}")
    async with sessionmaker() as session:
        urls = await collect_reply_images(
            bot=AsyncMock(),
            message=incoming,
            session=session,
            _download=download,
        )

    assert urls == ["url-a", "url-b", "url-c"]


@pytest.mark.asyncio
async def test_no_photo_in_reply_returns_empty(sessionmaker) -> None:
    chat_id = 888
    async with sessionmaker() as session:
        session.add(Message(
            chat_id=chat_id,
            message_id=10,
            user_id=1,
            text="plain",
            reply_to_id=None,
            date=datetime.utcnow(),
            is_bot=False,
        ))
        await session.commit()

    incoming = SimpleNamespace(
        chat=SimpleNamespace(id=chat_id),
        reply_to_message=SimpleNamespace(message_id=10, photo=None),
    )

    async with sessionmaker() as session:
        urls = await collect_reply_images(
            bot=AsyncMock(),
            message=incoming,
            session=session,
        )

    assert urls == []
