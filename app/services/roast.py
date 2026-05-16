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
