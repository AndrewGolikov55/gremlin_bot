from __future__ import annotations

import asyncio
from datetime import datetime
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
