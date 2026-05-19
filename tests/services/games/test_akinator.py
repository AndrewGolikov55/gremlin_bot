from __future__ import annotations

import unittest.mock as um
from unittest.mock import AsyncMock, create_autospec

import pytest
from aiogram.enums import ChatMemberStatus
from sqlalchemy import select

from app.models import AkinatorQuestion, AkinatorRound, User, UserMemoryProfile
from app.services.app_config import AppConfigService
from app.services.games.akinator import MAX_QUESTIONS, AkinatorService


def _make_bot():
    bot = AsyncMock()
    member = type("M", (), {})()
    member.status = ChatMemberStatus.MEMBER
    member.user = type("U", (), {})()
    member.user.first_name = "Андрей"
    member.user.username = "andrew"
    member.user.is_bot = False
    bot.get_chat_member = AsyncMock(return_value=member)
    bot.send_message = AsyncMock()
    return bot


def _make_svc(sessionmaker, *, bot=None, app_config=None):
    app_config = app_config or create_autospec(AppConfigService, instance=True)
    app_config.get_all = AsyncMock(return_value={})
    return AkinatorService(
        sessionmaker=sessionmaker, bot=bot or _make_bot(), app_config=app_config,
    )


async def _seed_profile(sessionmaker, *, chat_id=42, user_id=100, username="andrew"):
    async with sessionmaker() as session:
        session.add(User(tg_id=user_id, username=username))
        session.add(UserMemoryProfile(
            chat_id=chat_id, user_id=user_id,
            identity=["айтишник"],
            preferences=["кофе"],
            projects=[],
            boundaries=[],
            summary="любит писать на питоне",
        ))
        await session.commit()


@pytest.mark.asyncio
async def test_pick_target_skips_empty_profiles(sessionmaker):
    svc = _make_svc(sessionmaker)
    await _seed_profile(sessionmaker)
    target = await svc._pick_target(chat_id=42, exclude_user_id=999)
    assert target == 100


@pytest.mark.asyncio
async def test_start_creates_active_round(sessionmaker):
    svc = _make_svc(sessionmaker)
    await _seed_profile(sessionmaker)
    await svc.start(chat_id=42, initiator_id=200)
    async with sessionmaker() as session:
        rounds = (await session.execute(select(AkinatorRound))).scalars().all()
    assert len(rounds) == 1
    assert rounds[0].status == "active"
    assert rounds[0].target_user_id == 100


@pytest.mark.asyncio
async def test_start_announcement_has_no_unescaped_angle_brackets(sessionmaker):
    """Regression for v0.13.0 bug: '<вопрос>' in announcement was parsed as
    an HTML tag by Telegram (parse_mode=HTML globally) and crashed send_message."""
    svc = _make_svc(sessionmaker)
    await _seed_profile(sessionmaker)
    await svc.start(chat_id=42, initiator_id=200)

    # Find the announcement send_message call (the one that mentions the user how to ask)
    bot = svc.bot
    calls = bot.send_message.await_args_list
    announcements = [
        c for c in calls
        if "akinator_ask" in (c.args[1] if len(c.args) > 1 else c.kwargs.get("text", ""))
    ]
    assert announcements, "expected start announcement to be sent"
    text = announcements[0].args[1] if len(announcements[0].args) > 1 else announcements[0].kwargs["text"]
    # No raw <...word...> tags that Telegram would try to parse
    import re as _re
    bad = _re.findall(r"<[а-яёА-ЯЁ_]+>", text)
    assert not bad, f"unescaped angle-bracket tags would crash HTML parser: {bad}"


@pytest.mark.asyncio
async def test_ask_increments_counter_and_persists_answer(sessionmaker):
    svc = _make_svc(sessionmaker)
    await _seed_profile(sessionmaker)
    await svc.start(chat_id=42, initiator_id=200)

    async def fake_gen(messages, **kwargs):
        return "yes"

    with um.patch("app.services.games.akinator.llm_generate", fake_gen):
        await svc.ask(chat_id=42, asker_id=200, question="Он пьёт кофе?")

    async with sessionmaker() as session:
        rounds = (await session.execute(select(AkinatorRound))).scalars().all()
        questions = (await session.execute(select(AkinatorQuestion))).scalars().all()
    assert rounds[0].questions_asked == 1
    assert questions[0].answer == "yes"


@pytest.mark.asyncio
async def test_guess_correct_marks_won(sessionmaker):
    svc = _make_svc(sessionmaker)
    await _seed_profile(sessionmaker)
    await svc.start(chat_id=42, initiator_id=200)
    await svc.guess(chat_id=42, asker_id=200, target_username="@andrew")
    async with sessionmaker() as session:
        rounds = (await session.execute(select(AkinatorRound))).scalars().all()
    assert rounds[0].status == "won"
    assert rounds[0].winner_user_id == 200


@pytest.mark.asyncio
async def test_max_questions_marks_lost(sessionmaker):
    svc = _make_svc(sessionmaker)
    await _seed_profile(sessionmaker)
    await svc.start(chat_id=42, initiator_id=200)

    async def fake_gen(messages, **kwargs):
        return "no"

    with um.patch("app.services.games.akinator.llm_generate", fake_gen):
        for i in range(MAX_QUESTIONS):
            await svc.ask(chat_id=42, asker_id=200, question=f"q{i}")

    async with sessionmaker() as session:
        rounds = (await session.execute(select(AkinatorRound))).scalars().all()
    assert rounds[0].status == "lost"


@pytest.mark.asyncio
async def test_concurrent_asks_do_not_exceed_max_questions(sessionmaker):
    """Atomic question slot claim — running N+5 parallel asks must cap at MAX_QUESTIONS."""
    import asyncio
    svc = _make_svc(sessionmaker)
    await _seed_profile(sessionmaker)
    await svc.start(chat_id=42, initiator_id=200)

    async def fake_gen(messages, **kwargs):
        # tiny pause to invite reordering
        await asyncio.sleep(0)
        return "no"

    with um.patch("app.services.games.akinator.llm_generate", fake_gen):
        await asyncio.gather(*[
            svc.ask(chat_id=42, asker_id=200, question=f"q{i}")
            for i in range(MAX_QUESTIONS + 5)
        ])

    async with sessionmaker() as session:
        questions = (await session.execute(select(AkinatorQuestion))).scalars().all()
        rounds = (await session.execute(select(AkinatorRound))).scalars().all()
    assert len(questions) == MAX_QUESTIONS
    assert rounds[0].questions_asked == MAX_QUESTIONS
    assert rounds[0].status == "lost"


@pytest.mark.asyncio
async def test_llm_answer_parses_first_token_exactly(sessionmaker):
    """'not really' must NOT be parsed as 'no' via substring; first token wins."""
    svc = _make_svc(sessionmaker)

    # Direct probe of the answer parser via the underlying coroutine
    with um.patch(
        "app.services.games.akinator.llm_generate",
        AsyncMock(return_value="not really"),
    ):
        ans = await svc._llm_answer(system="s", user="u")
    assert ans == "unknown"

    with um.patch(
        "app.services.games.akinator.llm_generate",
        AsyncMock(return_value="yes, definitely"),
    ):
        ans = await svc._llm_answer(system="s", user="u")
    assert ans == "yes"

    with um.patch(
        "app.services.games.akinator.llm_generate",
        AsyncMock(return_value="approximately yes"),
    ):
        ans = await svc._llm_answer(system="s", user="u")
    assert ans == "unknown"


@pytest.mark.asyncio
async def test_stop_aborts_active_round(sessionmaker):
    svc = _make_svc(sessionmaker)
    await _seed_profile(sessionmaker)
    await svc.start(chat_id=42, initiator_id=200)
    await svc.stop(chat_id=42)
    async with sessionmaker() as session:
        row = (await session.execute(select(AkinatorRound))).scalar_one()
    assert row.status == "aborted"
    assert row.finished_at is not None


@pytest.mark.asyncio
async def test_stop_noop_when_no_active_round(sessionmaker):
    svc = _make_svc(sessionmaker)
    await svc.stop(chat_id=42)
    # bot was called once with the "Раунд не идёт" refusal
    bot = svc.bot
    assert bot.send_message.await_count == 1
    text = bot.send_message.await_args_list[0].args[1]
    assert "не идёт" in text.lower()


@pytest.mark.asyncio
async def test_start_when_active_reports_progress_not_generic(sessionmaker):
    """Regression for UX: second /akinator must mention question count and how to abort."""
    svc = _make_svc(sessionmaker)
    await _seed_profile(sessionmaker)
    await svc.start(chat_id=42, initiator_id=200)
    # Ask one question so the report shows N=1
    with um.patch("app.services.games.akinator.llm_generate", AsyncMock(return_value="yes")):
        await svc.ask(chat_id=42, asker_id=200, question="q1")

    svc.bot.send_message.reset_mock()
    # Second /akinator while one is already running.
    # The partial unique index that triggers IntegrityError is Postgres-only —
    # on SQLite we need to drive the IntegrityError path manually. So call the
    # reporter directly to test its content.
    await svc._report_active_round(42)

    bot = svc.bot
    assert bot.send_message.await_count == 1
    msg = bot.send_message.await_args_list[0].args[1]
    assert "1/" in msg and "20" in msg  # счётчик X/MAX
    assert "/akinator_stop" in msg
    assert "/akinator_guess" in msg


class TestTargetMeta:
    @pytest.mark.asyncio
    async def test_fetch_target_meta_returns_dataclass(self, sessionmaker):
        from app.services.games.akinator import TargetMeta
        svc = _make_svc(sessionmaker)
        await _seed_profile(sessionmaker)
        meta = await svc._fetch_target_meta(chat_id=42, user_id=100)
        assert isinstance(meta, TargetMeta)
        assert meta.display == "Андрей"
        assert meta.username == "andrew"
        # `_make_bot()` mocks status=ChatMemberStatus.MEMBER which str()s to 'member'
        assert meta.member_status == "member"
        assert meta.message_count_week == 0  # no Message rows in fixture

    @pytest.mark.asyncio
    async def test_fetch_target_meta_handles_missing_member(self, sessionmaker):
        from aiogram.exceptions import TelegramBadRequest

        bot = AsyncMock()
        bot.get_chat_member = AsyncMock(
            side_effect=TelegramBadRequest(method=None, message="not found")  # type: ignore[arg-type]
        )
        bot.send_message = AsyncMock()
        app_config = create_autospec(AppConfigService, instance=True)
        app_config.get_all = AsyncMock(return_value={})
        svc = AkinatorService(
            sessionmaker=sessionmaker, bot=bot, app_config=app_config,
        )
        meta = await svc._fetch_target_meta(chat_id=42, user_id=100)
        assert meta.display == "id100"
        assert meta.username is None
        assert meta.member_status is None
        assert meta.message_count_week == 0

    @pytest.mark.asyncio
    async def test_fetch_target_meta_counts_messages_within_week(self, sessionmaker):
        from datetime import datetime, timedelta

        from app.models import Message
        svc = _make_svc(sessionmaker)
        await _seed_profile(sessionmaker)
        # Seed messages: 3 within week, 1 old, 1 bot
        async with sessionmaker() as session:
            now = datetime.utcnow()
            for i, days in enumerate([1, 2, 3]):
                session.add(Message(
                    chat_id=42, message_id=1000 + i, user_id=100,
                    text=f"msg{i}", date=now - timedelta(days=days),
                    is_bot=False, reply_to_id=None,
                    tg_file_id=None, media_group_id=None,
                ))
            # Out of window
            session.add(Message(
                chat_id=42, message_id=2000, user_id=100, text="old",
                date=now - timedelta(days=10),
                is_bot=False, reply_to_id=None,
                tg_file_id=None, media_group_id=None,
            ))
            # Bot message — excluded
            session.add(Message(
                chat_id=42, message_id=3000, user_id=100, text="bot",
                date=now - timedelta(days=1),
                is_bot=True, reply_to_id=None,
                tg_file_id=None, media_group_id=None,
            ))
            await session.commit()
        meta = await svc._fetch_target_meta(chat_id=42, user_id=100)
        assert meta.message_count_week == 3

    @pytest.mark.asyncio
    async def test_target_meta_caches_by_round_id(self, sessionmaker):
        svc = _make_svc(sessionmaker)
        await _seed_profile(sessionmaker)
        # Two calls with same round_id — bot.get_chat_member called only once
        meta1 = await svc._target_meta(round_id=99, chat_id=42, user_id=100)
        meta2 = await svc._target_meta(round_id=99, chat_id=42, user_id=100)
        assert meta1 is meta2  # cached object identity
        assert svc.bot.get_chat_member.await_count == 1


class TestAskPromptIncludesMeta:
    @pytest.mark.asyncio
    async def test_ask_passes_meta_into_llm_prompt(self, sessionmaker):
        svc = _make_svc(sessionmaker)
        await _seed_profile(sessionmaker)
        await svc.start(chat_id=42, initiator_id=200)

        captured: dict[str, str] = {}

        async def fake_gen(messages, **kwargs):
            captured["system"] = messages[0]["content"]
            captured["user"] = messages[1]["content"]
            return "yes"

        with um.patch("app.services.games.akinator.llm_generate", fake_gen):
            await svc.ask(chat_id=42, asker_id=200, question="Это мужчина?")

        # Telegram-метаданные блок должен присутствовать в user-prompt
        assert "Telegram-метаданные" in captured["user"]
        assert "first_name: Андрей" in captured["user"]
        assert "username: @andrew" in captured["user"]
        assert "member_status: member" in captured["user"]
        assert "сообщений за неделю:" in captured["user"]
        # System prompt тоже обновлён — упоминает Telegram-метаданные
        assert "Telegram-метаданными" in captured["system"]


@pytest.mark.asyncio
async def test_recover_stale_expires_old_active(sessionmaker):
    from datetime import datetime, timedelta

    from app.services.games.akinator import MAX_ROUND_AGE

    svc = _make_svc(sessionmaker)
    await _seed_profile(sessionmaker)
    await svc.start(chat_id=42, initiator_id=200)
    async with sessionmaker() as session:
        row = (await session.execute(select(AkinatorRound))).scalar_one()
        row.started_at = datetime.utcnow() - MAX_ROUND_AGE - timedelta(hours=1)
        await session.commit()
    recovered = await svc.recover_stale()
    assert recovered == 1
    async with sessionmaker() as session:
        row = (await session.execute(select(AkinatorRound))).scalar_one()
    assert row.status == "expired"


class TestMetaCacheCleanup:
    @pytest.mark.asyncio
    async def test_cache_cleared_on_stop(self, sessionmaker):
        svc = _make_svc(sessionmaker)
        await _seed_profile(sessionmaker)
        await svc.start(chat_id=42, initiator_id=200)
        # Populate cache via one ask
        with um.patch("app.services.games.akinator.llm_generate", AsyncMock(return_value="yes")):
            await svc.ask(chat_id=42, asker_id=200, question="q?")
        assert len(svc._meta_cache) == 1
        await svc.stop(chat_id=42)
        assert len(svc._meta_cache) == 0

    @pytest.mark.asyncio
    async def test_cache_cleared_on_guess_won(self, sessionmaker):
        svc = _make_svc(sessionmaker)
        await _seed_profile(sessionmaker)
        await svc.start(chat_id=42, initiator_id=200)
        with um.patch("app.services.games.akinator.llm_generate", AsyncMock(return_value="yes")):
            await svc.ask(chat_id=42, asker_id=200, question="q?")
        assert len(svc._meta_cache) == 1
        await svc.guess(chat_id=42, asker_id=200, target_username="@andrew")
        assert len(svc._meta_cache) == 0

    @pytest.mark.asyncio
    async def test_cache_cleared_on_finish_lost(self, sessionmaker):
        svc = _make_svc(sessionmaker)
        await _seed_profile(sessionmaker)
        await svc.start(chat_id=42, initiator_id=200)
        with um.patch("app.services.games.akinator.llm_generate", AsyncMock(return_value="no")):
            for _ in range(MAX_QUESTIONS):
                await svc.ask(chat_id=42, asker_id=200, question="q?")
        # After MAX_QUESTIONS asks, _finish_lost called → cache must be empty
        assert len(svc._meta_cache) == 0

    @pytest.mark.asyncio
    async def test_recover_stale_clears_cache(self, sessionmaker):
        from datetime import datetime, timedelta
        from app.services.games.akinator import MAX_ROUND_AGE
        svc = _make_svc(sessionmaker)
        await _seed_profile(sessionmaker)
        await svc.start(chat_id=42, initiator_id=200)
        with um.patch("app.services.games.akinator.llm_generate", AsyncMock(return_value="yes")):
            await svc.ask(chat_id=42, asker_id=200, question="q?")
        # Backdate
        async with sessionmaker() as session:
            row = (await session.execute(select(AkinatorRound))).scalar_one()
            row.started_at = datetime.utcnow() - MAX_ROUND_AGE - timedelta(hours=1)
            await session.commit()
        assert len(svc._meta_cache) == 1
        await svc.recover_stale()
        assert len(svc._meta_cache) == 0
