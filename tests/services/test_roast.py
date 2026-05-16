from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, create_autospec

import pytest
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramBadRequest
from sqlalchemy import select

from app.models import Message, RoastRun, User, UserMemoryProfile
from app.services.app_config import AppConfigService
from app.services.persona import StylePromptService
from app.services.roast import RoastService
from app.services.settings import SettingsService


def _make_svc(sessionmaker, *, bot=None, personas=None, settings=None, app_config=None):
    return RoastService(
        sessionmaker=sessionmaker,
        bot=bot or AsyncMock(),
        personas=personas or create_autospec(StylePromptService, instance=True),
        settings=settings or create_autospec(SettingsService, instance=True),
        app_config=app_config or create_autospec(AppConfigService, instance=True),
    )


def test_get_lock_returns_same_instance(sessionmaker):
    svc = _make_svc(sessionmaker)
    lock_a = svc._get_lock(42)
    lock_b = svc._get_lock(42)
    lock_other = svc._get_lock(99)
    assert lock_a is lock_b
    assert lock_a is not lock_other
    assert isinstance(lock_a, asyncio.Lock)


@pytest.mark.asyncio
async def test_cooldown_returns_none_when_no_runs(sessionmaker):
    svc = _make_svc(sessionmaker)
    remaining = await svc._remaining_cooldown(chat_id=42, now=datetime(2026, 5, 16, 12, 0, 0))
    assert remaining is None


@pytest.mark.asyncio
async def test_cooldown_returns_none_when_last_run_older_than_24h(sessionmaker):
    chat_id = 42
    async with sessionmaker() as session:
        session.add(RoastRun(
            chat_id=chat_id, target_user_id=100, initiator_user_id=200,
            target_username="andrew",
            run_at=datetime(2026, 5, 15, 11, 0, 0),  # 25h ago
        ))
        await session.commit()
    svc = _make_svc(sessionmaker)
    remaining = await svc._remaining_cooldown(chat_id=chat_id, now=datetime(2026, 5, 16, 12, 0, 0))
    assert remaining is None


@pytest.mark.asyncio
async def test_cooldown_returns_remaining_when_within_24h(sessionmaker):
    chat_id = 42
    async with sessionmaker() as session:
        session.add(RoastRun(
            chat_id=chat_id, target_user_id=100, initiator_user_id=200,
            target_username="andrew",
            run_at=datetime(2026, 5, 16, 10, 0, 0),  # 2h ago
        ))
        await session.commit()
    svc = _make_svc(sessionmaker)
    remaining = await svc._remaining_cooldown(chat_id=chat_id, now=datetime(2026, 5, 16, 12, 0, 0))
    assert remaining is not None
    # 24h - 2h = 22h
    assert remaining == timedelta(hours=22)


@pytest.mark.asyncio
async def test_cooldown_only_considers_this_chat(sessionmaker):
    async with sessionmaker() as session:
        session.add(RoastRun(
            chat_id=99, target_user_id=1, initiator_user_id=2,
            target_username="x", run_at=datetime(2026, 5, 16, 11, 0, 0),
        ))
        await session.commit()
    svc = _make_svc(sessionmaker)
    remaining = await svc._remaining_cooldown(chat_id=42, now=datetime(2026, 5, 16, 12, 0, 0))
    assert remaining is None


@pytest.mark.asyncio
async def test_active_user_ids_returns_authors_with_text_in_7d(sessionmaker):
    chat_id = 42
    now = datetime(2026, 5, 16, 12, 0, 0)
    async with sessionmaker() as session:
        # In-window: user 100, 101
        session.add(Message(
            chat_id=chat_id, message_id=1, user_id=100, text="hi",
            reply_to_id=None, date=now - timedelta(days=1), is_bot=False,
        ))
        session.add(Message(
            chat_id=chat_id, message_id=2, user_id=101, text="yo",
            reply_to_id=None, date=now - timedelta(days=6), is_bot=False,
        ))
        # Bot — excluded
        session.add(Message(
            chat_id=chat_id, message_id=3, user_id=999, text="i am a bot",
            reply_to_id=None, date=now - timedelta(days=1), is_bot=True,
        ))
        # Out-of-window
        session.add(Message(
            chat_id=chat_id, message_id=4, user_id=102, text="old",
            reply_to_id=None, date=now - timedelta(days=8), is_bot=False,
        ))
        # Other chat
        session.add(Message(
            chat_id=77, message_id=5, user_id=103, text="hey",
            reply_to_id=None, date=now - timedelta(days=1), is_bot=False,
        ))
        await session.commit()

    svc = _make_svc(sessionmaker)
    ids = await svc._active_user_ids(chat_id=chat_id, now=now, exclude_user_id=None)
    assert sorted(ids) == [100, 101]


@pytest.mark.asyncio
async def test_active_user_ids_excludes_initiator(sessionmaker):
    chat_id = 42
    now = datetime(2026, 5, 16, 12, 0, 0)
    async with sessionmaker() as session:
        for uid in (100, 101, 200):
            session.add(Message(
                chat_id=chat_id, message_id=uid, user_id=uid, text="hi",
                reply_to_id=None, date=now - timedelta(days=1), is_bot=False,
            ))
        await session.commit()

    svc = _make_svc(sessionmaker)
    ids = await svc._active_user_ids(chat_id=chat_id, now=now, exclude_user_id=200)
    assert sorted(ids) == [100, 101]


@pytest.mark.asyncio
async def test_active_user_ids_ignores_empty_text(sessionmaker):
    chat_id = 42
    now = datetime(2026, 5, 16, 12, 0, 0)
    async with sessionmaker() as session:
        session.add(Message(
            chat_id=chat_id, message_id=1, user_id=100, text="",
            reply_to_id=None, date=now - timedelta(days=1), is_bot=False,
        ))
        session.add(Message(
            chat_id=chat_id, message_id=2, user_id=101, text="real",
            reply_to_id=None, date=now - timedelta(days=1), is_bot=False,
        ))
        await session.commit()

    svc = _make_svc(sessionmaker)
    ids = await svc._active_user_ids(chat_id=chat_id, now=now, exclude_user_id=None)
    assert ids == [101]


@pytest.mark.asyncio
async def test_resolve_random_picks_among_active(sessionmaker, monkeypatch):
    chat_id = 42
    now = datetime(2026, 5, 16, 12, 0, 0)
    async with sessionmaker() as session:
        for uid in (100, 101):
            session.add(Message(
                chat_id=chat_id, message_id=uid, user_id=uid, text="hey",
                reply_to_id=None, date=now - timedelta(days=1), is_bot=False,
            ))
        await session.commit()

    svc = _make_svc(sessionmaker)
    monkeypatch.setattr(
        "app.services.roast.random.choice",
        lambda seq: 101 if 101 in seq else seq[0],
    )

    uid, refusal = await svc._resolve_target(
        chat_id=chat_id, initiator_id=200, target_arg=None, now=now,
    )
    assert refusal is None
    assert uid == 101


@pytest.mark.asyncio
async def test_resolve_random_no_active_users(sessionmaker):
    chat_id = 42
    now = datetime(2026, 5, 16, 12, 0, 0)
    svc = _make_svc(sessionmaker)
    uid, refusal = await svc._resolve_target(
        chat_id=chat_id, initiator_id=200, target_arg=None, now=now,
    )
    assert uid is None
    assert refusal is not None
    assert "тишина" in refusal.lower() or "некого" in refusal.lower()


@pytest.mark.asyncio
async def test_resolve_random_only_initiator_active(sessionmaker):
    chat_id = 42
    now = datetime(2026, 5, 16, 12, 0, 0)
    async with sessionmaker() as session:
        session.add(Message(
            chat_id=chat_id, message_id=1, user_id=200, text="only me",
            reply_to_id=None, date=now - timedelta(days=1), is_bot=False,
        ))
        await session.commit()
    svc = _make_svc(sessionmaker)
    uid, refusal = await svc._resolve_target(
        chat_id=chat_id, initiator_id=200, target_arg=None, now=now,
    )
    assert uid is None
    assert refusal is not None


@pytest.mark.asyncio
async def test_resolve_explicit_username_succeeds(sessionmaker):
    chat_id = 42
    now = datetime(2026, 5, 16, 12, 0, 0)
    async with sessionmaker() as session:
        session.add(User(tg_id=100, username="andrew"))
        session.add(Message(
            chat_id=chat_id, message_id=1, user_id=100, text="recent",
            reply_to_id=None, date=now - timedelta(days=1), is_bot=False,
        ))
        await session.commit()
    svc = _make_svc(sessionmaker)
    uid, refusal = await svc._resolve_target(
        chat_id=chat_id, initiator_id=200, target_arg="@andrew", now=now,
    )
    assert refusal is None
    assert uid == 100


@pytest.mark.asyncio
async def test_resolve_explicit_username_unknown(sessionmaker):
    svc = _make_svc(sessionmaker)
    uid, refusal = await svc._resolve_target(
        chat_id=42, initiator_id=200, target_arg="@ghost",
        now=datetime(2026, 5, 16, 12, 0, 0),
    )
    assert uid is None
    assert refusal is not None
    assert "ghost" in refusal.lower() or "не знаю" in refusal.lower()


@pytest.mark.asyncio
async def test_resolve_explicit_username_inactive(sessionmaker):
    chat_id = 42
    now = datetime(2026, 5, 16, 12, 0, 0)
    async with sessionmaker() as session:
        session.add(User(tg_id=100, username="andrew"))
        # Last activity > 7 days ago
        session.add(Message(
            chat_id=chat_id, message_id=1, user_id=100, text="old",
            reply_to_id=None, date=now - timedelta(days=10), is_bot=False,
        ))
        await session.commit()
    svc = _make_svc(sessionmaker)
    uid, refusal = await svc._resolve_target(
        chat_id=chat_id, initiator_id=200, target_arg="@andrew", now=now,
    )
    assert uid is None
    assert refusal is not None
    assert "след" in refusal.lower() or "неделю" in refusal.lower()


@pytest.mark.asyncio
async def test_resolve_explicit_username_self_refused(sessionmaker):
    chat_id = 42
    now = datetime(2026, 5, 16, 12, 0, 0)
    async with sessionmaker() as session:
        session.add(User(tg_id=200, username="me"))
        session.add(Message(
            chat_id=chat_id, message_id=1, user_id=200, text="self",
            reply_to_id=None, date=now - timedelta(days=1), is_bot=False,
        ))
        await session.commit()
    svc = _make_svc(sessionmaker)
    uid, refusal = await svc._resolve_target(
        chat_id=chat_id, initiator_id=200, target_arg="@me", now=now,
    )
    assert uid is None
    assert refusal is not None
    assert "сам" in refusal.lower()


@pytest.mark.asyncio
async def test_collect_target_context_pulls_last_30_text_messages(sessionmaker):
    chat_id = 42
    target = 100
    async with sessionmaker() as session:
        for i in range(35):
            session.add(Message(
                chat_id=chat_id, message_id=i + 1, user_id=target,
                text=f"msg{i}", reply_to_id=None,
                date=datetime(2026, 5, 16, 0, 0, 0) + timedelta(minutes=i),
                is_bot=False,
            ))
        # Empty-text and bot messages — ignored
        session.add(Message(
            chat_id=chat_id, message_id=999, user_id=target, text="",
            reply_to_id=None, date=datetime(2026, 5, 17, 0, 0, 0), is_bot=False,
        ))
        await session.commit()

    bot = AsyncMock()
    member = type("M", (), {})()
    member.status = ChatMemberStatus.MEMBER
    member.user = type("U", (), {})()
    member.user.first_name = "Андрей"
    member.user.username = "andrew"
    member.user.is_bot = False
    bot.get_chat_member = AsyncMock(return_value=member)

    svc = _make_svc(sessionmaker, bot=bot)
    ctx = await svc._collect_target_context(chat_id=chat_id, user_id=target)
    assert len(ctx.messages) == 30
    # Oldest-to-newest order, slice = last 30 of 35 → msg5..msg34
    assert ctx.messages[0] == "msg5"
    assert ctx.messages[-1] == "msg34"
    assert ctx.display_name == "Андрей"
    assert ctx.username == "andrew"


@pytest.mark.asyncio
async def test_collect_target_context_includes_user_memory_profile(sessionmaker):
    chat_id = 42
    target = 100
    async with sessionmaker() as session:
        session.add(UserMemoryProfile(
            chat_id=chat_id, user_id=target,
            summary="ходячая ирония",
            identity=["разработчик", "котейничает"],
            preferences=["любит rust"],
            projects=["gremlin_bot"],
            boundaries=["не упоминать развод"],
        ))
        await session.commit()

    bot = AsyncMock()
    bot.get_chat_member = AsyncMock(
        side_effect=TelegramBadRequest(method=None, message="not found")  # type: ignore[arg-type]
    )

    svc = _make_svc(sessionmaker, bot=bot)
    ctx = await svc._collect_target_context(chat_id=chat_id, user_id=target)
    assert ctx.summary == "ходячая ирония"
    assert ctx.identity == ["разработчик", "котейничает"]
    assert ctx.preferences == ["любит rust"]
    assert ctx.projects == ["gremlin_bot"]
    assert ctx.boundaries == ["не упоминать развод"]
    assert ctx.messages == []
    # Fallback display name
    assert ctx.display_name == "id100"
    assert ctx.username is None


@pytest.mark.asyncio
async def test_collect_target_context_no_profile_returns_empty_lists(sessionmaker):
    chat_id = 42
    target = 100

    bot = AsyncMock()
    member = type("M", (), {})()
    member.status = ChatMemberStatus.MEMBER
    member.user = type("U", (), {})()
    member.user.first_name = "Семён"
    member.user.username = None
    member.user.is_bot = False
    bot.get_chat_member = AsyncMock(return_value=member)

    svc = _make_svc(sessionmaker, bot=bot)
    ctx = await svc._collect_target_context(chat_id=chat_id, user_id=target)
    assert ctx.summary is None
    assert ctx.identity == []
    assert ctx.preferences == []
    assert ctx.projects == []
    assert ctx.boundaries == []
    assert ctx.display_name == "Семён"
    assert ctx.username is None


@pytest.mark.asyncio
async def test_build_prompts_contains_persona_and_roast_rules(sessionmaker):
    personas = create_autospec(StylePromptService, instance=True)
    personas.get = AsyncMock(return_value="Ты гопник из 90х.")

    settings = create_autospec(SettingsService, instance=True)
    settings.get_all = AsyncMock(return_value={"style": "gopnik"})

    svc = _make_svc(sessionmaker, personas=personas, settings=settings)

    ctx = __import__("app.services.roast", fromlist=["_TargetContext"])._TargetContext(
        user_id=100, display_name="Андрей", username="andrew",
        messages=["сегодня я снова не выспался", "ладно, погнали кодить"],
        identity=["разработчик"],
        preferences=["любит rust"],
        projects=["gremlin_bot"],
        boundaries=["не упоминать развод"],
        summary="вечно усталый",
    )

    system, user = await svc._build_prompts(chat_id=42, ctx=ctx)

    # Persona is present
    assert "гопник" in system
    # Hard rules are present
    assert "Прожарка" in system or "прожарк" in system.lower()
    assert "3-5" in system or "три-пять" in system.lower() or "3–5" in system
    # User prompt has the profile + messages + hidden topics block
    assert "Андрей" in user
    assert "andrew" in user
    assert "разработчик" in user
    assert "любит rust" in user
    assert "gremlin_bot" in user
    assert "вечно усталый" in user
    assert "Hidden topics" in user
    assert "не упоминать развод" in user
    assert "сегодня я снова не выспался" in user
    assert "ладно, погнали кодить" in user
    # Roast call-to-action
    assert "Жарь" in user


@pytest.mark.asyncio
async def test_build_prompts_empty_profile_and_messages(sessionmaker):
    personas = create_autospec(StylePromptService, instance=True)
    personas.get = AsyncMock(return_value="Ты гопник.")
    settings = create_autospec(SettingsService, instance=True)
    settings.get_all = AsyncMock(return_value={"style": "gopnik"})

    svc = _make_svc(sessionmaker, personas=personas, settings=settings)

    ctx = __import__("app.services.roast", fromlist=["_TargetContext"])._TargetContext(
        user_id=100, display_name="Семён", username=None,
        messages=[], identity=[], preferences=[], projects=[],
        boundaries=[], summary=None,
    )
    system, user = await svc._build_prompts(chat_id=42, ctx=ctx)
    # Hidden topics block exists even when empty
    assert "Hidden topics" in user
    assert "—" in user
    # No-text fallback marker
    assert "нет текстовых сообщений" in user.lower()


@pytest.mark.asyncio
async def test_llm_call_returns_text_on_success(sessionmaker):
    app_config = create_autospec(AppConfigService, instance=True)
    app_config.get_all = AsyncMock(return_value={"llm_provider": "openrouter"})
    svc = _make_svc(sessionmaker, app_config=app_config)

    import unittest.mock as um
    captured: dict = {}

    async def fake_generate(messages, **kwargs):
        captured["messages"] = messages
        captured["kwargs"] = kwargs
        return "  Жёсткий текст.  "

    with um.patch("app.services.roast.llm_generate", fake_generate):
        text = await svc._llm_call(system="SYS", user="USR")

    assert text == "  Жёсткий текст.  "
    assert captured["messages"][0]["role"] == "system"
    assert captured["messages"][0]["content"] == "SYS"
    assert captured["messages"][1]["role"] == "user"
    assert captured["messages"][1]["content"] == "USR"
    assert captured["kwargs"]["max_tokens"] == 280
    assert captured["kwargs"]["temperature"] == 0.95


@pytest.mark.asyncio
async def test_llm_call_returns_none_on_llm_error(sessionmaker):
    from app.services.llm.client import LLMError

    app_config = create_autospec(AppConfigService, instance=True)
    app_config.get_all = AsyncMock(return_value={})
    svc = _make_svc(sessionmaker, app_config=app_config)

    async def fake_generate(messages, **kwargs):
        raise LLMError("all providers down")

    import unittest.mock as um
    with um.patch("app.services.roast.llm_generate", fake_generate):
        text = await svc._llm_call(system="SYS", user="USR")
    assert text is None


@pytest.mark.asyncio
async def test_llm_call_returns_none_on_unexpected_error(sessionmaker):
    app_config = create_autospec(AppConfigService, instance=True)
    app_config.get_all = AsyncMock(return_value={})
    svc = _make_svc(sessionmaker, app_config=app_config)

    async def fake_generate(messages, **kwargs):
        raise RuntimeError("boom")

    import unittest.mock as um
    with um.patch("app.services.roast.llm_generate", fake_generate):
        text = await svc._llm_call(system="SYS", user="USR")
    assert text is None


@pytest.mark.asyncio
async def test_run_happy_path_sends_and_persists(sessionmaker):
    chat_id = 42
    initiator = 200
    target = 100
    now = datetime(2026, 5, 16, 12, 0, 0)

    async with sessionmaker() as session:
        session.add(User(tg_id=target, username="andrew"))
        session.add(Message(
            chat_id=chat_id, message_id=1, user_id=target, text="привет",
            reply_to_id=None, date=now - timedelta(days=1), is_bot=False,
        ))
        await session.commit()

    bot = AsyncMock()
    member = type("M", (), {})()
    member.status = ChatMemberStatus.MEMBER
    member.user = type("U", (), {})()
    member.user.first_name = "Андрей"
    member.user.username = "andrew"
    member.user.is_bot = False
    bot.get_chat_member = AsyncMock(return_value=member)
    bot.send_message = AsyncMock()

    personas = create_autospec(StylePromptService, instance=True)
    personas.get = AsyncMock(return_value="persona-prompt")
    settings = create_autospec(SettingsService, instance=True)
    settings.get_all = AsyncMock(return_value={"style": "gopnik"})
    app_config = create_autospec(AppConfigService, instance=True)
    app_config.get_all = AsyncMock(return_value={})

    svc = _make_svc(
        sessionmaker, bot=bot, personas=personas, settings=settings, app_config=app_config,
    )

    import unittest.mock as um

    async def fake_gen(messages, **kwargs):
        return "Жёстко прожарен."

    with um.patch("app.services.roast.llm_generate", fake_gen), \
         um.patch("app.services.roast.asyncio.sleep", AsyncMock()):
        await svc.run(
            chat_id=chat_id, initiator_id=initiator,
            target_arg="@andrew", now=now,
        )

    # 2 messages: announcement + roast body
    assert bot.send_message.await_count == 2
    announce_text = bot.send_message.call_args_list[0].kwargs["text"]
    body_text = bot.send_message.call_args_list[1].kwargs["text"]
    assert announce_text.startswith("🔥 Жарю")
    assert "@andrew" in announce_text
    assert "Жёстко прожарен" in body_text

    async with sessionmaker() as session:
        rows = (await session.execute(select(RoastRun))).scalars().all()
    assert len(rows) == 1
    assert rows[0].chat_id == chat_id
    assert rows[0].target_user_id == target
    assert rows[0].initiator_user_id == initiator
    assert rows[0].target_username == "andrew"


@pytest.mark.asyncio
async def test_run_blocks_when_cooldown_active(sessionmaker):
    chat_id = 42
    now = datetime(2026, 5, 16, 12, 0, 0)
    async with sessionmaker() as session:
        session.add(RoastRun(
            chat_id=chat_id, target_user_id=1, initiator_user_id=2,
            target_username="prev", run_at=now - timedelta(hours=3),
        ))
        await session.commit()

    bot = AsyncMock()
    bot.send_message = AsyncMock()
    svc = _make_svc(sessionmaker, bot=bot)

    await svc.run(chat_id=chat_id, initiator_id=200, target_arg=None, now=now)

    bot.send_message.assert_awaited_once()
    call = bot.send_message.call_args
    sent_text = call.kwargs.get("text", call.args[1] if len(call.args) > 1 else None)
    assert "21" in sent_text  # 24h - 3h = 21h remaining
    # Никакой новой записи
    async with sessionmaker() as session:
        rows = (await session.execute(select(RoastRun))).scalars().all()
    assert len(rows) == 1  # только предыдущая


@pytest.mark.asyncio
async def test_run_sends_refusal_when_target_unknown(sessionmaker):
    bot = AsyncMock()
    bot.send_message = AsyncMock()
    svc = _make_svc(sessionmaker, bot=bot)

    await svc.run(
        chat_id=42, initiator_id=200, target_arg="@ghost",
        now=datetime(2026, 5, 16, 12, 0, 0),
    )

    bot.send_message.assert_awaited_once()
    async with sessionmaker() as session:
        rows = (await session.execute(select(RoastRun))).scalars().all()
    assert rows == []  # отказ не съедает кулдаун


@pytest.mark.asyncio
async def test_run_llm_failure_sends_fallback_and_skips_cooldown(sessionmaker):
    chat_id = 42
    initiator = 200
    target = 100
    now = datetime(2026, 5, 16, 12, 0, 0)

    async with sessionmaker() as session:
        session.add(User(tg_id=target, username="andrew"))
        session.add(Message(
            chat_id=chat_id, message_id=1, user_id=target, text="hi",
            reply_to_id=None, date=now - timedelta(days=1), is_bot=False,
        ))
        await session.commit()

    bot = AsyncMock()
    member = type("M", (), {})()
    member.status = ChatMemberStatus.MEMBER
    member.user = type("U", (), {})()
    member.user.first_name = "Андрей"
    member.user.username = "andrew"
    member.user.is_bot = False
    bot.get_chat_member = AsyncMock(return_value=member)
    bot.send_message = AsyncMock()

    personas = create_autospec(StylePromptService, instance=True)
    personas.get = AsyncMock(return_value="persona")
    settings = create_autospec(SettingsService, instance=True)
    settings.get_all = AsyncMock(return_value={"style": "gopnik"})
    app_config = create_autospec(AppConfigService, instance=True)
    app_config.get_all = AsyncMock(return_value={})

    svc = _make_svc(
        sessionmaker, bot=bot, personas=personas, settings=settings, app_config=app_config,
    )

    import unittest.mock as um

    from app.services.llm.client import LLMError

    async def fake_gen(messages, **kwargs):
        raise LLMError("all down")

    with um.patch("app.services.roast.llm_generate", fake_gen), \
         um.patch("app.services.roast.asyncio.sleep", AsyncMock()):
        await svc.run(
            chat_id=chat_id, initiator_id=initiator,
            target_arg="@andrew", now=now,
        )

    # 2 messages: announcement + fallback
    assert bot.send_message.await_count == 2
    announce_text = bot.send_message.call_args_list[0].kwargs["text"]
    fallback_text = bot.send_message.call_args_list[1].kwargs["text"]
    assert announce_text.startswith("🔥 Жарю")
    assert ("LLM" in fallback_text or "обмороке" in fallback_text.lower()
            or "позже" in fallback_text.lower())

    # КУЛДАУН НЕ ЗАПИСАН
    async with sessionmaker() as session:
        rows = (await session.execute(select(RoastRun))).scalars().all()
    assert rows == []


@pytest.mark.asyncio
async def test_run_per_chat_lock_serializes_concurrent_calls(sessionmaker):
    """Second concurrent call sees the first run's RoastRun → cooldown refusal."""
    chat_id = 42
    initiator = 200
    target = 100
    now = datetime(2026, 5, 16, 12, 0, 0)

    async with sessionmaker() as session:
        session.add(User(tg_id=target, username="andrew"))
        session.add(Message(
            chat_id=chat_id, message_id=1, user_id=target, text="hi",
            reply_to_id=None, date=now - timedelta(days=1), is_bot=False,
        ))
        await session.commit()

    bot = AsyncMock()
    member = type("M", (), {})()
    member.status = ChatMemberStatus.MEMBER
    member.user = type("U", (), {})()
    member.user.first_name = "Андрей"
    member.user.username = "andrew"
    member.user.is_bot = False
    bot.get_chat_member = AsyncMock(return_value=member)
    bot.send_message = AsyncMock()

    personas = create_autospec(StylePromptService, instance=True)
    personas.get = AsyncMock(return_value="persona")
    settings = create_autospec(SettingsService, instance=True)
    settings.get_all = AsyncMock(return_value={"style": "gopnik"})
    app_config = create_autospec(AppConfigService, instance=True)
    app_config.get_all = AsyncMock(return_value={})

    svc = _make_svc(
        sessionmaker, bot=bot, personas=personas, settings=settings, app_config=app_config,
    )

    import unittest.mock as um
    started = asyncio.Event()
    proceed = asyncio.Event()

    async def slow_gen(messages, **kwargs):
        started.set()
        await proceed.wait()
        return "Жёстко."

    async def runner():
        with um.patch("app.services.roast.llm_generate", slow_gen), \
             um.patch("app.services.roast.asyncio.sleep", AsyncMock()):
            await svc.run(
                chat_id=chat_id, initiator_id=initiator,
                target_arg="@andrew", now=now,
            )

    first = asyncio.create_task(runner())
    await started.wait()
    # Second call should queue on the same lock
    second = asyncio.create_task(svc.run(
        chat_id=chat_id, initiator_id=initiator,
        target_arg="@andrew", now=now,
    ))
    # Let the first complete
    proceed.set()
    await asyncio.gather(first, second)

    # Exactly one RoastRun row
    async with sessionmaker() as session:
        rows = (await session.execute(select(RoastRun))).scalars().all()
    assert len(rows) == 1
    # 3 send_messages: announcement + body from first call, cooldown refusal from second
    assert bot.send_message.await_count == 3
