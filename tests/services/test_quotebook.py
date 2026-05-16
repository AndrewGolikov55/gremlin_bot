from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from app.services.quotebook import QuotebookService, _week_start_for


def test_week_start_for_sunday_evening_returns_previous_monday():
    # Воскресенье 17 мая 2026, 20:00 МСК. Прошлая неделя пн..вс = 11..17 мая.
    now = datetime(2026, 5, 17, 20, 0, 0, tzinfo=ZoneInfo("Europe/Moscow"))
    assert _week_start_for(now) == date(2026, 5, 11)


def test_week_start_for_monday_morning_returns_previous_monday():
    # Понедельник 18 мая 2026, 09:00 МСК — это уже новая неделя, прошлая = 11..17 мая.
    now = datetime(2026, 5, 18, 9, 0, 0, tzinfo=ZoneInfo("Europe/Moscow"))
    assert _week_start_for(now) == date(2026, 5, 11)


def test_week_start_for_sunday_morning_returns_two_mondays_ago():
    # Воскресенье 17 мая 2026, 09:00 МСК — текущая неделя ещё идёт (пн..вс = 11..17).
    # Cron в 20:00; здесь покрываем краевой случай: ручной вызов утром тех же суток.
    # Прошлая неделя = пн..вс предыдущая (4..10 мая) — week_start=4 мая.
    now = datetime(2026, 5, 17, 9, 0, 0, tzinfo=ZoneInfo("Europe/Moscow"))
    assert _week_start_for(now) == date(2026, 5, 4)


def test_week_start_for_requires_tz_aware():
    naive = datetime(2026, 5, 17, 20, 0, 0)
    with pytest.raises(ValueError):
        _week_start_for(naive)


@pytest.mark.asyncio
async def test_service_lock_is_per_chat(sessionmaker):
    from unittest.mock import AsyncMock, create_autospec

    from app.services.app_config import AppConfigService
    from app.services.settings import SettingsService

    svc = QuotebookService(
        sessionmaker=sessionmaker,
        bot=AsyncMock(),
        settings=create_autospec(SettingsService, instance=True),
        app_config=create_autospec(AppConfigService, instance=True),
    )
    lock1 = svc._get_lock(1)
    lock2 = svc._get_lock(2)
    lock1_again = svc._get_lock(1)
    assert lock1 is lock1_again
    assert lock1 is not lock2
