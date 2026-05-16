from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, create_autospec

import pytest
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramBadRequest

from app.models import Message, RoastRun, UserMemoryProfile
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
