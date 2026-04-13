from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.message import Message
from app.services.reply_images import MAX_REPLY_IMAGES, collect_reply_images


def _reply_msg(chat_id: int = 100, reply_to_id: int | None = 50, reply_photo: list | None = None) -> SimpleNamespace:
    reply_to = None
    if reply_to_id is not None:
        reply_to = SimpleNamespace(
            message_id=reply_to_id,
            photo=reply_photo,
        )
    return SimpleNamespace(
        chat=SimpleNamespace(id=chat_id),
        reply_to_message=reply_to,
    )


async def _seed_message(sessionmaker: async_sessionmaker[AsyncSession], **fields: Any) -> None:
    async with sessionmaker() as session:
        session.add(Message(
            chat_id=fields["chat_id"],
            message_id=fields["message_id"],
            user_id=fields.get("user_id", 1),
            text=fields.get("text", "[photo]"),
            reply_to_id=None,
            date=fields.get("date", datetime.utcnow()),
            is_bot=False,
            tg_file_id=fields.get("tg_file_id"),
            media_group_id=fields.get("media_group_id"),
        ))
        await session.commit()


@pytest.mark.asyncio
async def test_returns_empty_when_no_reply(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    msg = SimpleNamespace(chat=SimpleNamespace(id=100), reply_to_message=None)
    async with sessionmaker() as session:
        result = await collect_reply_images(bot=AsyncMock(), message=msg, session=session)  # type: ignore[arg-type]
    assert result == []


@pytest.mark.asyncio
async def test_single_photo_from_db(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    await _seed_message(sessionmaker, chat_id=100, message_id=50, tg_file_id="fid-1")
    msg = _reply_msg()
    download = AsyncMock(return_value="data:image/jpeg;base64,AAA")
    async with sessionmaker() as session:
        result = await collect_reply_images(
            bot=AsyncMock(),
            message=msg,  # type: ignore[arg-type]
            session=session,
            _download=download,
        )
    assert result == ["data:image/jpeg;base64,AAA"]
    download.assert_awaited_once()


@pytest.mark.asyncio
async def test_album_returns_all_file_ids_in_order(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    for mid, fid in [(50, "a"), (51, "b"), (52, "c")]:
        await _seed_message(sessionmaker, chat_id=100, message_id=mid,
                            tg_file_id=fid, media_group_id="G1")
    msg = _reply_msg(reply_to_id=50)
    download = AsyncMock(side_effect=lambda _bot, fid: f"data:image/jpeg;base64,{fid.upper()}")
    async with sessionmaker() as session:
        result = await collect_reply_images(
            bot=AsyncMock(),
            message=msg,  # type: ignore[arg-type]
            session=session,
            _download=download,
        )
    assert result == [
        "data:image/jpeg;base64,A",
        "data:image/jpeg;base64,B",
        "data:image/jpeg;base64,C",
    ]


@pytest.mark.asyncio
async def test_album_truncates_to_max(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    for i in range(15):
        await _seed_message(sessionmaker, chat_id=100, message_id=50 + i,
                            tg_file_id=f"f{i}", media_group_id="big")
    msg = _reply_msg(reply_to_id=50)
    download = AsyncMock(side_effect=lambda _bot, fid: f"url-{fid}")
    async with sessionmaker() as session:
        result = await collect_reply_images(
            bot=AsyncMock(),
            message=msg,  # type: ignore[arg-type]
            session=session,
            _download=download,
        )
    assert len(result) == MAX_REPLY_IMAGES
    assert result[0] == "url-f0"
    assert result[-1] == f"url-f{MAX_REPLY_IMAGES - 1}"


@pytest.mark.asyncio
async def test_partial_download_failure_drops_failed_entries(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    for mid, fid in [(50, "ok1"), (51, "bad"), (52, "ok2")]:
        await _seed_message(sessionmaker, chat_id=100, message_id=mid,
                            tg_file_id=fid, media_group_id="G2")
    msg = _reply_msg(reply_to_id=50)

    async def fake_download(_bot, fid):
        if fid == "bad":
            return None
        return f"url-{fid}"

    async with sessionmaker() as session:
        result = await collect_reply_images(
            bot=AsyncMock(),
            message=msg,  # type: ignore[arg-type]
            session=session,
            _download=fake_download,
        )
    assert result == ["url-ok1", "url-ok2"]


@pytest.mark.asyncio
async def test_fallback_to_reply_to_message_photo(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    reply_photo = [SimpleNamespace(file_id="live-fid", file_size=50_000, width=800, height=600)]
    msg = _reply_msg(reply_to_id=999, reply_photo=reply_photo)
    download = AsyncMock(return_value="data:image/jpeg;base64,LIVE")
    async with sessionmaker() as session:
        result = await collect_reply_images(
            bot=AsyncMock(),
            message=msg,  # type: ignore[arg-type]
            session=session,
            _download=download,
        )
    assert result == ["data:image/jpeg;base64,LIVE"]
    download.assert_awaited_once()
    assert download.await_args is not None
    assert download.await_args.args[1] == "live-fid"
