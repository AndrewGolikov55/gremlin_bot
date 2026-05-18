from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, create_autospec

import pytest
import unittest.mock as um
from aiogram.enums import ChatMemberStatus

from app.models import Message, User
from app.services.app_config import AppConfigService
from app.services.persona import StylePromptService
from app.services.quick_games import QuickGameService
from app.services.settings import SettingsService


def _make_svc(sessionmaker, *, bot=None, personas=None, settings=None, app_config=None):
    bot = bot or AsyncMock()
    personas = personas or create_autospec(StylePromptService, instance=True)
    personas.get = AsyncMock(return_value="persona")
    settings = settings or create_autospec(SettingsService, instance=True)
    settings.get_all = AsyncMock(return_value={"style": "gopnik"})
    app_config = app_config or create_autospec(AppConfigService, instance=True)
    app_config.get_all = AsyncMock(return_value={})
    return QuickGameService(
        sessionmaker=sessionmaker, bot=bot,
        personas=personas, settings=settings, app_config=app_config,
    )


def _make_bot_member(*, first_name="Андрей", username="andrew", is_bot=False):
    bot = AsyncMock()
    member = type("M", (), {})()
    member.status = ChatMemberStatus.MEMBER
    member.user = type("U", (), {})()
    member.user.first_name = first_name
    member.user.username = username
    member.user.is_bot = is_bot
    bot.get_chat_member = AsyncMock(return_value=member)
    bot.send_message = AsyncMock()
    return bot


async def _seed_user_with_messages(sessionmaker, *, tg_id=100, username="andrew",
                                   chat_id=42, now: datetime, count=3):
    async with sessionmaker() as session:
        session.add(User(tg_id=tg_id, username=username))
        for i in range(count):
            session.add(Message(
                chat_id=chat_id, message_id=i + 1, user_id=tg_id, text=f"msg {i}",
                reply_to_id=None, date=now - timedelta(days=1, hours=i), is_bot=False,
            ))
        await session.commit()


@pytest.mark.asyncio
async def test_truth_sends_message_with_target_mention(sessionmaker):
    now = datetime(2026, 5, 18, 12, 0, 0)
    await _seed_user_with_messages(sessionmaker, now=now)
    bot = _make_bot_member()
    svc = _make_svc(sessionmaker, bot=bot)

    async def fake_gen(messages, **kwargs):
        return "Расскажи, почему ты до сих пор не закрыл рабочую таску с прошлой пятницы?"

    with um.patch("app.services.quick_games.llm_generate", fake_gen):
        await svc.run_truth_or_dare(
            chat_id=42, initiator_id=200, target_arg="@andrew", now=now,
        )

    bot.send_message.assert_awaited_once()
    sent_text = bot.send_message.call_args.kwargs["text"]
    assert "@andrew" in sent_text
    assert "Расскажи" in sent_text


@pytest.mark.asyncio
async def test_horoscope_sends_personalised(sessionmaker):
    now = datetime(2026, 5, 18, 12, 0, 0)
    await _seed_user_with_messages(sessionmaker, now=now)
    bot = _make_bot_member()
    svc = _make_svc(sessionmaker, bot=bot)

    async def fake_gen(messages, **kwargs):
        return "Сегодня Меркурий не в твоей пользе, не открывай новые таски."

    with um.patch("app.services.quick_games.llm_generate", fake_gen):
        await svc.run_horoscope(
            chat_id=42, initiator_id=200, target_arg="@andrew", now=now,
        )

    sent_text = bot.send_message.call_args.kwargs["text"]
    assert "Гороскоп" in sent_text
    assert "Меркурий" in sent_text


@pytest.mark.asyncio
async def test_fortune_no_target_no_persona(sessionmaker):
    bot = AsyncMock()
    bot.send_message = AsyncMock()
    svc = _make_svc(sessionmaker, bot=bot)

    captured: dict[str, list] = {}

    async def fake_gen(messages, **kwargs):
        captured["messages"] = messages
        return "Не каждая пельмешка доплывает до тарелки."

    with um.patch("app.services.quick_games.llm_generate", fake_gen):
        await svc.run_fortune(
            chat_id=42, initiator_id=200, now=datetime(2026, 5, 18, 12, 0, 0),
        )

    sent_text = bot.send_message.call_args.kwargs["text"]
    assert "🥠" in sent_text
    assert "пельмешка" in sent_text
    # Persona prompt must NOT be merged into system
    assert "persona" not in captured["messages"][0]["content"]


@pytest.mark.asyncio
async def test_wisdom_attributes_to_active_member(sessionmaker):
    now = datetime(2026, 5, 18, 12, 0, 0)
    await _seed_user_with_messages(sessionmaker, now=now)
    bot = _make_bot_member()
    svc = _make_svc(sessionmaker, bot=bot)

    async def fake_gen(messages, **kwargs):
        return "Каждое утро начинается с того, что я снова не понимаю свой код."

    with um.patch("app.services.quick_games.llm_generate", fake_gen):
        await svc.run_wisdom(
            chat_id=42, initiator_id=200, now=now,
        )

    sent_text = bot.send_message.call_args.kwargs["text"]
    assert "@andrew" in sent_text
    assert "Каждое утро" in sent_text


@pytest.mark.asyncio
async def test_predict_targets_replied_user_via_random_when_arg_missing(sessionmaker):
    now = datetime(2026, 5, 18, 12, 0, 0)
    await _seed_user_with_messages(sessionmaker, now=now)
    bot = _make_bot_member()
    svc = _make_svc(sessionmaker, bot=bot)

    async def fake_gen(messages, **kwargs):
        return "Через неделю ты случайно станешь экспертом по альпаководству."

    with um.patch("app.services.quick_games.llm_generate", fake_gen):
        await svc.run_predict(
            chat_id=42, initiator_id=200, target_arg=None, now=now,
        )

    sent_text = bot.send_message.call_args.kwargs["text"]
    assert "Предсказание" in sent_text
    assert "альпаководству" in sent_text


@pytest.mark.asyncio
async def test_build_context_unknown_user_returns_refusal(sessionmaker):
    now = datetime(2026, 5, 18, 12, 0, 0)
    bot = _make_bot_member()
    svc = _make_svc(sessionmaker, bot=bot)
    result = await svc.build_context(
        chat_id=42, initiator_id=200, target_arg="@ghost", now=now,
    )
    assert isinstance(result, str)
    assert "ghost" in result or "знаю" in result
