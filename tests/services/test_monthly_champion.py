from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from app.services.monthly_champion import MonthlyChampionService, _previous_period  # noqa: F401


def test_previous_period_basic():
    # 1 мая 12:00 MSK → period [2026-04-01, 2026-05-01)
    now = datetime(2026, 5, 1, 12, 0, 0, tzinfo=ZoneInfo("Europe/Moscow"))
    start, end_excl = _previous_period(now)
    assert start == date(2026, 4, 1)
    assert end_excl == date(2026, 5, 1)


def test_previous_period_january():
    # 1 января 12:00 MSK → period [2025-12-01, 2026-01-01)
    now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=ZoneInfo("Europe/Moscow"))
    start, end_excl = _previous_period(now)
    assert start == date(2025, 12, 1)
    assert end_excl == date(2026, 1, 1)


def test_previous_period_mid_month():
    # 15 мая 03:00 MSK → period [2026-04-01, 2026-05-01)
    now = datetime(2026, 5, 15, 3, 0, 0, tzinfo=ZoneInfo("Europe/Moscow"))
    start, end_excl = _previous_period(now)
    assert start == date(2026, 4, 1)
    assert end_excl == date(2026, 5, 1)


def test_previous_period_requires_aware_datetime():
    naive = datetime(2026, 5, 1, 12, 0, 0)
    with pytest.raises(ValueError):
        _previous_period(naive)
