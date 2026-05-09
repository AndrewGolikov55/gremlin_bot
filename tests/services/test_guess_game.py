from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable
from unittest.mock import AsyncMock, MagicMock

import pytest
from pytest import MonkeyPatch
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import GuessRound, RouletteScoreAdjustment
from app.models.message import Message
from app.services.guess_game import (
    GuessGameService,
    LLMPick,
    NoCandidatesError,
    PreparedRound,
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


def test_parse_llm_pick_extracts_from_codeblock_no_lang_tag() -> None:
    raw = "```\n{\"author_user_id\": 1, \"message_id\": 42}\n```"
    pick = parse_llm_pick(raw, valid_authors={1}, valid_message_ids={42})
    assert pick is not None
    assert pick.author_user_id == 1


def test_parse_llm_pick_extracts_from_codeblock_other_lang_tag() -> None:
    raw = "```python\n{\"author_user_id\": 1, \"message_id\": 42}\n```"
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


def _svc(
    sessionmaker_: async_sessionmaker[AsyncSession],
    *,
    llm_pick_fn: Callable[..., Awaitable[Any]] | None = None,
    display_name_fn: Callable[[int, int], Awaitable[str]] | None = None,
    display_user_fn: Callable[[int, int], Awaitable[tuple[str | None, str | None]]] | None = None,
) -> GuessGameService:
    svc = GuessGameService.__new__(GuessGameService)
    svc.sessionmaker = sessionmaker_
    svc.bot = None
    svc.app_config = MagicMock()
    svc.app_config.get_all = AsyncMock(return_value={})
    svc._display_name = display_name_fn or AsyncMock(side_effect=lambda chat_id, user_id: f"user{user_id}")
    svc._display_user = display_user_fn or AsyncMock(return_value=(None, None))
    svc._llm_pick = llm_pick_fn or AsyncMock(return_value=None)
    svc._rng = random.Random(42)
    return svc


@pytest.mark.asyncio
async def test_prepare_round_raises_when_no_authors(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    svc = _svc(sessionmaker)
    with pytest.raises(NoCandidatesError):
        await svc.prepare_round(chat_id=-200, now=datetime.utcnow())


@pytest.mark.asyncio
async def test_prepare_round_degrades_to_2_options_when_only_2_authors(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    chat_id = -201
    base = "a" * 60
    async with sessionmaker() as session:
        for uid in (1, 2):
            for i in range(5):
                session.add(_msg(chat_id, uid * 100 + i, uid, base + f" {uid}-{i}"))
        await session.commit()

    svc = _svc(sessionmaker)
    round_ = await svc.prepare_round(chat_id=chat_id, now=datetime.utcnow())
    assert isinstance(round_, PreparedRound)
    assert len(round_.option_user_ids) == 2
    assert round_.author_user_id in round_.option_user_ids
    assert round_.correct_option_id == round_.option_user_ids.index(round_.author_user_id)
    assert round_.selection_mode == "random_fallback"


@pytest.mark.asyncio
async def test_prepare_round_uses_4_options_when_enough_authors(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    chat_id = -202
    base = "a" * 60
    async with sessionmaker() as session:
        for uid in (1, 2, 3, 4, 5):
            for i in range(5):
                session.add(_msg(chat_id, uid * 100 + i, uid, base + f" {uid}-{i}"))
        await session.commit()

    svc = _svc(sessionmaker)
    round_ = await svc.prepare_round(chat_id=chat_id, now=datetime.utcnow())
    assert len(round_.option_user_ids) == 4
    assert len(set(round_.option_user_ids)) == 4
    assert round_.author_user_id in round_.option_user_ids


@pytest.mark.asyncio
async def test_prepare_round_uses_llm_pick_when_returned(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    chat_id = -203
    base = "a" * 60
    async with sessionmaker() as session:
        for uid in (1, 2):
            for i in range(5):
                session.add(_msg(chat_id, uid * 100 + i, uid, base + f" {uid}-{i}"))
        await session.commit()

    async def fake_llm_pick(*args: object, **kwargs: object) -> LLMPick:
        return LLMPick(author_user_id=2, message_id=200, reason="cringe")

    svc = _svc(sessionmaker, llm_pick_fn=fake_llm_pick)
    round_ = await svc.prepare_round(chat_id=chat_id, now=datetime.utcnow())
    assert round_.author_user_id == 2
    assert round_.source_message_id == 200
    assert round_.selection_mode == "llm"


@pytest.mark.asyncio
async def test_prepare_round_post_filter_falls_back_to_random(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    chat_id = -204
    base = "a" * 60
    async with sessionmaker() as session:
        # User 1: message_id=100 contains "Андрей" (their first name)
        session.add(_msg(chat_id, 100, 1, "Я Андрей и сегодня всё было ужасно как всегда"))
        for i in range(1, 5):
            session.add(_msg(chat_id, 100 + i, 1, base + f" 1-{i}"))
        for i in range(5):
            session.add(_msg(chat_id, 200 + i, 2, base + f" 2-{i}"))
        await session.commit()

    async def fake_llm_pick(*args: object, **kwargs: object) -> LLMPick:
        return LLMPick(author_user_id=1, message_id=100, reason="cringe")

    svc = _svc(
        sessionmaker,
        llm_pick_fn=fake_llm_pick,
        display_user_fn=AsyncMock(return_value=("andryuha", "Андрей")),
    )
    round_ = await svc.prepare_round(chat_id=chat_id, now=datetime.utcnow())
    assert round_.selection_mode == "random_fallback"


def _make_round_row(chat_id: int, *, started_at: datetime | None = None, poll_id: str = "p1", first_winner: int | None = None) -> GuessRound:
    return GuessRound(
        chat_id=chat_id,
        poll_id=poll_id,
        chat_message_id=1,
        source_chat_id=chat_id,
        source_message_id=1,
        author_user_id=1,
        correct_option_id=0,
        option_user_ids=[1, 2, 3, 4],
        started_at=started_at or datetime.utcnow(),
        first_winner_user_id=first_winner,
        selection_mode="llm",
    )


@pytest.mark.asyncio
async def test_can_start_today_blocks_when_round_today(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    chat_id = -300
    async with sessionmaker() as session:
        session.add(_make_round_row(chat_id, started_at=datetime.utcnow(), poll_id="p-cs1"))
        await session.commit()

    svc = _svc(sessionmaker)
    assert (await svc.can_start_today(chat_id=chat_id, now=datetime.utcnow())) is False


@pytest.mark.asyncio
async def test_can_start_today_allows_when_round_yesterday(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    chat_id = -301
    async with sessionmaker() as session:
        session.add(_make_round_row(
            chat_id,
            started_at=datetime.utcnow() - timedelta(days=1, hours=12),
            poll_id="p-cs2",
        ))
        await session.commit()

    svc = _svc(sessionmaker)
    assert (await svc.can_start_today(chat_id=chat_id, now=datetime.utcnow())) is True


@pytest.mark.asyncio
async def test_record_first_winner_inserts_adjustment(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    chat_id = -302
    async with sessionmaker() as session:
        row = _make_round_row(chat_id, poll_id="p-record-1")
        session.add(row)
        await session.commit()
        round_id = row.id

    svc = _svc(sessionmaker)
    result = await svc.record_first_winner(round_id=round_id, user_id=42, now=datetime.utcnow())
    assert result is True

    async with sessionmaker() as session:
        adj = (await session.execute(
            select(RouletteScoreAdjustment).where(RouletteScoreAdjustment.user_id == 42)
        )).scalar_one()
        assert adj.delta == -1
        assert adj.reason == "guess_first_winner"
        assert adj.source_id == round_id
        assert adj.chat_id == chat_id


@pytest.mark.asyncio
async def test_record_first_winner_is_idempotent(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    chat_id = -303
    async with sessionmaker() as session:
        row = _make_round_row(chat_id, poll_id="p-record-2")
        session.add(row)
        await session.commit()
        round_id = row.id

    svc = _svc(sessionmaker)
    first = await svc.record_first_winner(round_id=round_id, user_id=42, now=datetime.utcnow())
    second = await svc.record_first_winner(round_id=round_id, user_id=99, now=datetime.utcnow())

    assert first is True
    assert second is False

    async with sessionmaker() as session:
        adjs = (await session.execute(
            select(RouletteScoreAdjustment).where(RouletteScoreAdjustment.source_id == round_id)
        )).scalars().all()
        assert len(adjs) == 1
        assert adjs[0].user_id == 42


@pytest.mark.asyncio
async def test_persist_round_writes_row(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    svc = _svc(sessionmaker)
    prepared = PreparedRound(
        chat_id=-304,
        author_user_id=7,
        source_message_id=123,
        text="some text",
        option_user_ids=[1, 2, 7, 9],
        option_labels=["A", "B", "C", "D"],
        correct_option_id=2,
        selection_mode="llm",
    )
    rid = await svc.persist_round(prepared, poll_id="poll-A", chat_message_id=999)

    async with sessionmaker() as session:
        row = (await session.execute(select(GuessRound).where(GuessRound.id == rid))).scalar_one()
        assert row.poll_id == "poll-A"
        assert row.chat_message_id == 999
        assert row.author_user_id == 7
        assert row.option_user_ids == [1, 2, 7, 9]
        assert row.correct_option_id == 2
        assert row.selection_mode == "llm"


@pytest.mark.asyncio
async def test_find_round_by_poll(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    chat_id = -305
    async with sessionmaker() as session:
        row = _make_round_row(chat_id, poll_id="p-find-1")
        session.add(row)
        await session.commit()
        expected_id = row.id

    svc = _svc(sessionmaker)
    found = await svc.find_round_by_poll("p-find-1")
    assert found is not None
    assert found.id == expected_id

    missing = await svc.find_round_by_poll("does-not-exist")
    assert missing is None


@pytest.mark.asyncio
async def test_llm_pick_real_calls_generate_with_compact_payload(monkeypatch: MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    async def fake_generate(messages: Any, **kwargs: Any) -> str:
        captured["messages"] = list(messages)
        captured["kwargs"] = kwargs
        return '{"author_user_id": 1, "message_id": 100, "reason": "ok"}'

    from app.services import guess_game as gg
    monkeypatch.setattr(gg, "llm_generate", fake_generate)

    author_messages = {
        1: [_msg(-100, 100, 1, "x" * 60), _msg(-100, 101, 1, "y" * 60)],
        2: [_msg(-100, 200, 2, "z" * 60)],
    }

    svc = GuessGameService.__new__(GuessGameService)
    svc.app_config = MagicMock()
    svc.app_config.get_all = AsyncMock(return_value={"llm_provider": "openrouter"})
    pick = await svc._llm_pick_real(author_messages, chat_id=-100)
    assert pick == LLMPick(author_user_id=1, message_id=100, reason="ok")

    msgs: list[Any] = captured["messages"]
    assert msgs[0]["role"] == "system"
    payload_str: str = msgs[-1]["content"]
    assert "100" in payload_str
    assert "candidates" in payload_str


@pytest.mark.asyncio
async def test_llm_pick_real_returns_none_on_llm_error(monkeypatch: MonkeyPatch) -> None:
    from app.services import guess_game as gg

    async def boom(*args: Any, **kwargs: Any) -> str:
        raise gg.LLMError("nope")

    monkeypatch.setattr(gg, "llm_generate", boom)

    svc = GuessGameService.__new__(GuessGameService)
    svc.app_config = MagicMock()
    svc.app_config.get_all = AsyncMock(return_value={})
    pick = await svc._llm_pick_real(
        {1: [_msg(-100, 1, 1, "x" * 60)]}, chat_id=-100,
    )
    assert pick is None


@pytest.mark.asyncio
async def test_llm_pick_real_returns_none_on_empty_candidates() -> None:
    svc = GuessGameService.__new__(GuessGameService)
    svc.app_config = MagicMock()
    svc.app_config.get_all = AsyncMock(return_value={})
    pick = await svc._llm_pick_real({}, chat_id=-100)
    assert pick is None
