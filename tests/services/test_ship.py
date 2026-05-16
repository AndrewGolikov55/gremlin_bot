from __future__ import annotations

from unittest.mock import AsyncMock, create_autospec

import pytest

from app.services.app_config import AppConfigService
from app.services.persona import StylePromptService
from app.services.settings import SettingsService
from app.services.ship import ShipService


def _make_service(sessionmaker):
    return ShipService(
        sessionmaker=sessionmaker,
        bot=AsyncMock(),
        settings=create_autospec(SettingsService, instance=True),
        app_config=create_autospec(AppConfigService, instance=True),
        personas=create_autospec(StylePromptService, instance=True),
    )


def test_canonicalize_pair_orders_user_ids(sessionmaker):
    assert ShipService.canonicalize_pair(200, 100) == (100, 200)
    assert ShipService.canonicalize_pair(100, 200) == (100, 200)
    assert ShipService.canonicalize_pair(5, 5) == (5, 5)


@pytest.mark.asyncio
async def test_service_creates_lock_per_chat(sessionmaker):
    svc = _make_service(sessionmaker)
    l1 = svc._get_lock(42)
    l2 = svc._get_lock(42)
    l3 = svc._get_lock(43)
    assert l1 is l2
    assert l1 is not l3
