"""Weekly «Афоризм недели» service.

Runs every Sunday 20:00 MSK (APScheduler cron). For each active chat:
  1. close previous open round (stop_poll, count votes, pick winner with
     optional drama runoff, +1 RouletteScoreAdjustment, announce)
  2. open new round (collect candidates, optional LLM selection, send_poll,
     persist QuoteWeekRound row)

Single-process deployment is assumed. Per-chat asyncio.Lock guards races
between cron tick, manual triggers, and startup catch-up inside one process.
The UNIQUE(chat_id, week_start) constraint is a last-line defence — if two
processes ever raced, only one row would persist, but both Telegram polls
would already have been sent.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..models import Chat, Message, QuoteWeekRound, RouletteScoreAdjustment
from .app_config import AppConfigService
from .llm.client import LLMError, LLMRateLimitError, resolve_llm_options
from .llm.client import generate as llm_generate
from .settings import SettingsService

logger = logging.getLogger("bot.quotebook")

MoscowTZ = ZoneInfo("Europe/Moscow")
PER_CHAT_SLEEP_SEC = 0.5
DRAMA_PAUSE_SEC = 2
LLM_MAX_TOKENS = 100
LLM_TEMPERATURE = 0.6
TG_POLL_OPTION_LIMIT = 100
TG_POLL_QUESTION_LIMIT = 290
WINDOW_DAYS = 7
MIN_CANDIDATES = 3
MAX_POLL_OPTIONS = 6
LLM_INPUT_TOP_N = 50
CATCH_UP_STALE_HOURS = 24
MSG_MIN_LEN = 20
MSG_MAX_LEN = 300


def _week_start_for(now: datetime) -> date:
    """Return Monday of the most-recently-completed calendar week.

    `now` must be timezone-aware. Examples (Europe/Moscow):
        Sun 2026-05-17 20:00 → 2026-05-11 (week 11..17 just completed at cron-time)
        Mon 2026-05-18 09:00 → 2026-05-11 (last full week 11..17)
        Sun 2026-05-17 09:00 → 2026-05-04 (current week 11..17 not finished yet)

    Logic: a week is considered «completed» once Sunday 20:00 (cron-time) has
    passed. Before that boundary, the previous Monday-of-current-week minus 7
    days is the answer; after that, the current week's Monday.
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    local = now.astimezone(now.tzinfo)
    # Find the most recent Sunday 20:00 boundary that is <= now.
    # weekday(): Mon=0..Sun=6. Days since most recent Sunday (today if Sun else past).
    days_since_sun = (local.weekday() - 6) % 7
    last_sun_date = local.date() - timedelta(days=days_since_sun)
    # If today is Sunday but before 20:00, last boundary is the previous Sunday.
    if days_since_sun == 0 and local.hour < 20:
        last_sun_date = last_sun_date - timedelta(days=7)
    # week_start = Monday of the week that just ended on last_sun_date.
    return last_sun_date - timedelta(days=6)


@dataclass(frozen=True)
class Candidate:
    message_id: int
    user_id: int
    text: str
    reply_count: int
    date: datetime


@dataclass(frozen=True)
class PollOption:
    text: str
    author_user_id: int
    source_message_id: int


def score_candidate(c: Candidate, *, max_reply: int, now: datetime) -> float:
    """Heuristic score in [0.0, 1.0] used for top-50 cut and LLM-fallback ranking.

    Weights: 0.6 reply, 0.3 length (capped at 200), 0.1 recency (linear decay over 7d).
    """
    reply_norm = 0.0
    if max_reply > 0:
        reply_norm = min(c.reply_count, max_reply) / max_reply
    length_norm = min(len(c.text), 200) / 200.0
    age_seconds = (now - c.date).total_seconds()
    window_seconds = WINDOW_DAYS * 24 * 3600
    recency_norm = max(0.0, 1.0 - (age_seconds / window_seconds))
    return 0.6 * reply_norm + 0.3 * length_norm + 0.1 * recency_norm


class QuotebookService:
    def __init__(
        self,
        *,
        sessionmaker: async_sessionmaker[AsyncSession],
        bot: Bot,
        settings: SettingsService,
        app_config: AppConfigService,
    ) -> None:
        self.sessionmaker = sessionmaker
        self.bot = bot
        self.settings = settings
        self.app_config = app_config
        self._chat_locks: dict[int, asyncio.Lock] = {}

    def _get_lock(self, chat_id: int) -> asyncio.Lock:
        lock = self._chat_locks.get(chat_id)
        if lock is None:
            lock = asyncio.Lock()
            self._chat_locks[chat_id] = lock
        return lock

    async def _get_bot_username(self) -> str | None:
        try:
            me = await self.bot.get_me()
        except Exception:  # noqa: BLE001 — display-side fallback; never raises out
            logger.exception("quotebook: bot.get_me failed")
            return None
        return getattr(me, "username", None)

    async def collect_candidates(
        self,
        *,
        chat_id: int,
        now: datetime,
    ) -> list[Candidate]:
        """Return Message-derived candidates from the last 7 days.

        Filters: is_bot=False, len(text) in [20, 300], text does not start with
        '/' nor with '@<bot_username>'. `now` is naive (treated as UTC) and the
        7-day window is `[now - 7d, now)` to match how Message.date is stored.
        """
        window_start = now - timedelta(days=WINDOW_DAYS)
        bot_username = await self._get_bot_username()
        bot_mention_prefix = f"@{bot_username.lower()}" if bot_username else None

        async with self.sessionmaker() as session:
            stmt = (
                select(Message)
                .where(
                    Message.chat_id == chat_id,
                    Message.is_bot.is_(False),
                    Message.date >= window_start,
                    Message.date < now,
                )
                .order_by(Message.date.asc())
            )
            rows = (await session.execute(stmt)).scalars().all()

            # reply_count: count other in-window messages whose reply_to_id matches
            # each candidate's message_id. Done in-memory over the same window
            # to keep the implementation simple and dialect-agnostic.
            reply_counts: dict[int, int] = {}
            for m in rows:
                if m.reply_to_id is not None:
                    reply_counts[m.reply_to_id] = reply_counts.get(m.reply_to_id, 0) + 1

            candidates: list[Candidate] = []
            for m in rows:
                text = (m.text or "").strip()
                if len(text) < MSG_MIN_LEN or len(text) > MSG_MAX_LEN:
                    continue
                if text.startswith("/"):
                    continue
                if bot_mention_prefix and text.lower().startswith(bot_mention_prefix):
                    continue
                candidates.append(Candidate(
                    message_id=m.message_id,
                    user_id=m.user_id,
                    text=text,
                    reply_count=reply_counts.get(m.message_id, 0),
                    date=m.date,
                ))
            return candidates
