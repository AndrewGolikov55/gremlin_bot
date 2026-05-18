from __future__ import annotations

from unittest.mock import AsyncMock, create_autospec

import pytest
import unittest.mock as um
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
