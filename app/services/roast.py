from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass
from datetime import datetime, timedelta

from aiogram import Bot
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramBadRequest
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..models import Message, RoastRun, UserMemoryProfile
from .app_config import AppConfigService
from .llm.client import LLMError, LLMRateLimitError, resolve_llm_options
from .llm.client import generate as llm_generate
from .persona import DEFAULT_STYLE_KEY, StylePromptService
from .settings import SettingsService

logger = logging.getLogger(__name__)

COOLDOWN = timedelta(hours=24)
ACTIVE_WINDOW = timedelta(days=7)
MAX_MESSAGES = 30
LLM_MAX_TOKENS = 280
LLM_TEMPERATURE = 0.95

# Single-process deployment assumed: per-chat asyncio.Lock guards concurrent
# /roast invocations inside one Python process. With multi-replica deployment
# this lock does not synchronise — two replicas could simultaneously pass the
# cooldown check, run two LLM calls and insert two roast_runs rows. If we ever
# move to multi-replica, switch to a Postgres advisory lock or Redis-based
# per-chat lock here.


@dataclass(frozen=True)
class _TargetContext:
    user_id: int
    display_name: str
    username: str | None
    messages: list[str]
    identity: list[str]
    preferences: list[str]
    projects: list[str]
    boundaries: list[str]
    summary: str | None


class RoastService:
    def __init__(
        self,
        *,
        sessionmaker: async_sessionmaker[AsyncSession],
        bot: Bot,
        personas: StylePromptService,
        settings: SettingsService,
        app_config: AppConfigService,
    ) -> None:
        self.sessionmaker = sessionmaker
        self.bot = bot
        self.personas = personas
        self.settings = settings
        self.app_config = app_config
        self._chat_locks: dict[int, asyncio.Lock] = {}

    def _get_lock(self, chat_id: int) -> asyncio.Lock:
        lock = self._chat_locks.get(chat_id)
        if lock is None:
            lock = asyncio.Lock()
            self._chat_locks[chat_id] = lock
        return lock

    async def _remaining_cooldown(
        self, *, chat_id: int, now: datetime
    ) -> timedelta | None:
        """Return time left in the 24h cooldown, or None if a fresh roast is allowed."""
        cutoff = now - COOLDOWN
        async with self.sessionmaker() as session:
            stmt = (
                select(RoastRun.run_at)
                .where(RoastRun.chat_id == chat_id, RoastRun.run_at >= cutoff)
                .order_by(desc(RoastRun.run_at))
                .limit(1)
            )
            last = (await session.execute(stmt)).scalar_one_or_none()
        if last is None:
            return None
        elapsed = now - last
        remaining = COOLDOWN - elapsed
        if remaining <= timedelta(0):
            return None
        return remaining

    async def _active_user_ids(
        self,
        *,
        chat_id: int,
        now: datetime,
        exclude_user_id: int | None,
    ) -> list[int]:
        """User IDs with at least one non-empty text message in the last 7 days.

        Bots (`is_bot=True`) are excluded. `exclude_user_id` is removed from the result
        if provided (used to drop the initiator for random target selection).
        """
        cutoff = now - ACTIVE_WINDOW
        stmt = (
            select(Message.user_id)
            .where(
                Message.chat_id == chat_id,
                Message.is_bot.is_(False),
                Message.date >= cutoff,
                func.length(Message.text) > 0,
            )
            .group_by(Message.user_id)
        )
        async with self.sessionmaker() as session:
            rows = (await session.execute(stmt)).all()
        ids = [int(row[0]) for row in rows]
        if exclude_user_id is not None:
            ids = [uid for uid in ids if uid != exclude_user_id]
        return ids
