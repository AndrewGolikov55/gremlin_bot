from __future__ import annotations

from datetime import datetime

import pytest

from app.models.message import Message


@pytest.mark.asyncio
async def test_message_persists_photo_refs(sessionmaker) -> None:
    async with sessionmaker() as session:
        msg = Message(
            chat_id=100,
            message_id=1,
            user_id=42,
            text="[photo] hello",
            reply_to_id=None,
            date=datetime.utcnow(),
            is_bot=False,
            tg_file_id="AgACAgQ_stub",
            media_group_id="album-123",
        )
        session.add(msg)
        await session.commit()
        await session.refresh(msg)
        assert msg.tg_file_id == "AgACAgQ_stub"
        assert msg.media_group_id == "album-123"


@pytest.mark.asyncio
async def test_message_photo_refs_default_null(sessionmaker) -> None:
    async with sessionmaker() as session:
        msg = Message(
            chat_id=100,
            message_id=2,
            user_id=42,
            text="plain text",
            reply_to_id=None,
            date=datetime.utcnow(),
            is_bot=False,
        )
        session.add(msg)
        await session.commit()
        await session.refresh(msg)
        assert msg.tg_file_id is None
        assert msg.media_group_id is None
