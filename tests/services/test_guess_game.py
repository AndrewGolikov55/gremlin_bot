from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.message import Message
from app.services.guess_game import (
    LLMPick,
    _moscow_midnight,
    parse_llm_pick,
    pick_candidate_authors,
    pick_messages_for_author,
    text_contains_author_identity,
)


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


def test_moscow_midnight_treats_naive_as_utc() -> None:
    # 22:00 UTC on May 8 = 01:00 MSK on May 9.
    # The Moscow midnight for that moment is May 9, 00:00 MSK = May 8, 21:00 UTC.
    naive_utc = datetime(2026, 5, 8, 22, 0, 0)
    midnight = _moscow_midnight(naive_utc)
    assert midnight == datetime(2026, 5, 8, 21, 0, 0)


def test_moscow_midnight_handles_aware_utc() -> None:
    aware = datetime(2026, 5, 8, 22, 0, 0, tzinfo=timezone.utc)
    midnight = _moscow_midnight(aware)
    assert midnight == datetime(2026, 5, 8, 21, 0, 0)


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


def test_parse_llm_pick_valid() -> None:
    raw = '{"author_user_id": 5, "message_id": 42, "reason": "very cringe"}'
    valid_authors = {5, 9}
    valid_message_ids = {42, 43}
    pick = parse_llm_pick(raw, valid_authors=valid_authors, valid_message_ids=valid_message_ids)
    assert pick == LLMPick(author_user_id=5, message_id=42, reason="very cringe")


def test_parse_llm_pick_invalid_json_returns_none() -> None:
    assert parse_llm_pick("not json", valid_authors={1}, valid_message_ids={1}) is None


def test_parse_llm_pick_unknown_author_returns_none() -> None:
    raw = '{"author_user_id": 999, "message_id": 42}'
    assert parse_llm_pick(raw, valid_authors={1, 2}, valid_message_ids={42}) is None


def test_parse_llm_pick_unknown_message_returns_none() -> None:
    raw = '{"author_user_id": 1, "message_id": 999}'
    assert parse_llm_pick(raw, valid_authors={1}, valid_message_ids={42}) is None


def test_parse_llm_pick_extracts_from_codeblock() -> None:
    raw = "```json\n{\"author_user_id\": 1, \"message_id\": 42}\n```"
    pick = parse_llm_pick(raw, valid_authors={1}, valid_message_ids={42})
    assert pick is not None
    assert pick.author_user_id == 1


def test_text_contains_author_identity_username_match() -> None:
    assert text_contains_author_identity(
        "как сказал andryuha, всё пропало",
        username="andryuha",
        first_name="Андрей",
    )


def test_text_contains_author_identity_first_name_match() -> None:
    assert text_contains_author_identity(
        "ну а Андрей опять опоздал",
        username=None,
        first_name="Андрей",
    )


def test_text_contains_author_identity_no_match() -> None:
    assert not text_contains_author_identity(
        "обычное сообщение без идентификации автора",
        username="andryuha",
        first_name="Андрей",
    )
