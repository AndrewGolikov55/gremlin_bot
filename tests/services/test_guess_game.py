from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.message import Message
from app.services.guess_game import pick_candidate_authors, pick_messages_for_author


def _msg(chat_id: int, message_id: int, user_id: int, text: str, *, days_ago: int = 1, is_bot: bool = False, tg_file_id: str | None = None) -> Message:
    return Message(
        chat_id=chat_id,
        message_id=message_id,
        user_id=user_id,
        text=text,
        reply_to_id=None,
        date=datetime.utcnow() - timedelta(days=days_ago),
        is_bot=is_bot,
        tg_file_id=tg_file_id,
        media_group_id=None,
    )


@pytest.mark.asyncio
async def test_pick_messages_filters_short(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    chat_id = -100
    async with sessionmaker() as session:
        session.add(_msg(chat_id, 1, 5, "коротко"))   # < 30
        session.add(_msg(chat_id, 2, 5, "x" * 50))    # OK
        await session.commit()

    async with sessionmaker() as session:
        msgs = await pick_messages_for_author(session, chat_id, user_id=5, now=datetime.utcnow())
    assert len(msgs) == 1
    assert msgs[0].message_id == 2


@pytest.mark.asyncio
async def test_pick_messages_filters_command_url_mention_bot_media(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    chat_id = -101
    base = "x" * 60
    async with sessionmaker() as session:
        session.add(_msg(chat_id, 1, 5, "/cmd " + base))                # command
        session.add(_msg(chat_id, 2, 5, base + " https://example.com")) # URL
        session.add(_msg(chat_id, 3, 5, base + " @somebody"))           # mention
        session.add(_msg(chat_id, 4, 5, base, is_bot=True))             # bot
        session.add(_msg(chat_id, 5, 5, base, tg_file_id="abc"))        # has media
        session.add(_msg(chat_id, 6, 5, base + " t.me/group"))          # tg link
        session.add(_msg(chat_id, 7, 5, base))                          # OK
        await session.commit()

    async with sessionmaker() as session:
        msgs = await pick_messages_for_author(session, chat_id, user_id=5, now=datetime.utcnow())
    ids = {m.message_id for m in msgs}
    assert ids == {7}


@pytest.mark.asyncio
async def test_pick_messages_filters_today(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    chat_id = -102
    base = "y" * 60
    async with sessionmaker() as session:
        session.add(_msg(chat_id, 1, 5, base, days_ago=0))  # today (skip)
        session.add(_msg(chat_id, 2, 5, base, days_ago=2))  # ok
        await session.commit()

    async with sessionmaker() as session:
        msgs = await pick_messages_for_author(session, chat_id, user_id=5, now=datetime.utcnow())
    assert {m.message_id for m in msgs} == {2}


@pytest.mark.asyncio
async def test_pick_messages_within_30d_window(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    chat_id = -103
    base = "z" * 60
    async with sessionmaker() as session:
        session.add(_msg(chat_id, 1, 5, base, days_ago=40))  # too old
        session.add(_msg(chat_id, 2, 5, base, days_ago=10))  # ok
        await session.commit()

    async with sessionmaker() as session:
        msgs = await pick_messages_for_author(session, chat_id, user_id=5, now=datetime.utcnow())
    assert {m.message_id for m in msgs} == {2}


@pytest.mark.asyncio
async def test_pick_authors_requires_min_5_eligible_messages(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    chat_id = -104
    base = "x" * 60
    async with sessionmaker() as session:
        for i in range(5):
            session.add(_msg(chat_id, 100 + i, 1, base + f" {i}"))
        for i in range(4):
            session.add(_msg(chat_id, 200 + i, 2, base + f" {i}"))
        await session.commit()

    async with sessionmaker() as session:
        authors = await pick_candidate_authors(session, chat_id, now=datetime.utcnow())
    assert authors == [1]
