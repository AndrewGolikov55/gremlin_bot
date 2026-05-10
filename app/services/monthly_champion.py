from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .app_config import AppConfigService
from .roulette import RouletteService
from .settings import SettingsService

logger = logging.getLogger(__name__)

MoscowTZ = ZoneInfo("Europe/Moscow")
CATCH_UP_DAY_LIMIT = 7
PER_CHAT_SLEEP_SEC = 0.5
DRAMA_PAUSE_SEC = 2
LLM_MAX_TOKENS = 250


def _previous_period(now: datetime) -> tuple[date, date]:
    """Returns (period_start, period_end_excl) for the calendar month BEFORE `now`'s month.

    `now` must be timezone-aware. The period is computed in `now.tzinfo`'s calendar.
    Example: now=2026-05-01 → (2026-04-01, 2026-05-01).
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    current_month_first = now.replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    ).date()
    last_day_of_prev = current_month_first - timedelta(days=1)
    period_start = last_day_of_prev.replace(day=1)
    return period_start, current_month_first


class MonthlyChampionService:
    def __init__(
        self,
        *,
        sessionmaker: async_sessionmaker[AsyncSession],
        bot: Bot,
        roulette: RouletteService,
        settings: SettingsService,
        app_config: AppConfigService,
    ) -> None:
        self.sessionmaker = sessionmaker
        self.bot = bot
        self.roulette = roulette
        self.settings = settings
        self.app_config = app_config
        self._chat_locks: dict[int, asyncio.Lock] = {}

    def _get_lock(self, chat_id: int) -> asyncio.Lock:
        lock = self._chat_locks.get(chat_id)
        if lock is None:
            lock = asyncio.Lock()
            self._chat_locks[chat_id] = lock
        return lock
