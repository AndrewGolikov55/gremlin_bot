from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from aiogram import Bot
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .app_config import AppConfigService
from .persona import StylePromptService
from .settings import SettingsService

logger = logging.getLogger(__name__)

WINDOW_DAYS = 30
CACHE_TTL = timedelta(hours=24)
LLM_MAX_TOKENS = 200
LLM_TEMPERATURE = 0.9

WEIGHT_REPLY = 0.35
WEIGHT_MENTION = 0.15
WEIGHT_COACTIVITY = 0.25
WEIGHT_PREF = 0.25


@dataclass(frozen=True)
class ShipMetrics:
    reply_count: int
    mention_count: int
    co_active_days: int
    pref_overlap_keywords: list[str]
    reply_rate: float
    mention_rate: float
    co_activity: float
    pref_overlap: float


@dataclass(frozen=True)
class ShipOutcome:
    score: int
    rendered_text: str
    cached: bool


class ShipService:
    def __init__(
        self,
        *,
        sessionmaker: async_sessionmaker[AsyncSession],
        bot: Bot,
        settings: SettingsService,
        app_config: AppConfigService,
        personas: StylePromptService,
    ) -> None:
        self.sessionmaker = sessionmaker
        self.bot = bot
        self.settings = settings
        self.app_config = app_config
        self.personas = personas
        self._chat_locks: dict[int, asyncio.Lock] = {}

    def _get_lock(self, chat_id: int) -> asyncio.Lock:
        lock = self._chat_locks.get(chat_id)
        if lock is None:
            lock = asyncio.Lock()
            self._chat_locks[chat_id] = lock
        return lock

    @staticmethod
    def canonicalize_pair(a: int, b: int) -> tuple[int, int]:
        """Return (min(a,b), max(a,b)) so the storage key is order-independent."""
        if a <= b:
            return a, b
        return b, a

    async def _reply_stats(
        self,
        session: AsyncSession,
        *,
        chat_id: int,
        a: int,
        b: int,
    ) -> tuple[int, int]:
        """Return (mutual_reply_count, denominator=min(A_total, B_total)) over the 30d window."""
        from sqlalchemy import and_, func, select

        from ..models import Message

        cutoff = datetime.utcnow() - timedelta(days=WINDOW_DAYS)

        # totals per user in window (exclude bot messages)
        totals_stmt = (
            select(Message.user_id, func.count().label("cnt"))
            .where(
                Message.chat_id == chat_id,
                Message.is_bot.is_(False),
                Message.date >= cutoff,
                Message.user_id.in_([a, b]),
            )
            .group_by(Message.user_id)
        )
        totals = dict((row.user_id, int(row.cnt)) for row in (await session.execute(totals_stmt)).all())
        a_total = totals.get(a, 0)
        b_total = totals.get(b, 0)

        # Self-join: child.reply_to_id == parent.message_id (same chat)
        Parent = Message.__table__.alias("parent")
        Child = Message.__table__.alias("child")
        reply_stmt = (
            select(func.count())
            .select_from(
                Child.join(
                    Parent,
                    and_(
                        Child.c.chat_id == Parent.c.chat_id,
                        Child.c.reply_to_id == Parent.c.message_id,
                    ),
                )
            )
            .where(
                Child.c.chat_id == chat_id,
                Child.c.date >= cutoff,
                Child.c.is_bot.is_(False),
                Parent.c.is_bot.is_(False),
                # (author=A, replying-to-B) OR (author=B, replying-to-A)
                ((Child.c.user_id == a) & (Parent.c.user_id == b))
                | ((Child.c.user_id == b) & (Parent.c.user_id == a)),
            )
        )
        reply_count = int((await session.execute(reply_stmt)).scalar() or 0)
        denom = min(a_total, b_total)
        return reply_count, denom

    async def _resolve_username(
        self,
        session: AsyncSession,
        *,
        chat_id: int,
        user_id: int,
    ) -> str | None:
        """Best-effort current username for a chat participant. Returns lowercase username sans '@'."""
        from sqlalchemy import select

        from ..models import RouletteParticipant, User

        stmt = (
            select(RouletteParticipant.username)
            .where(
                RouletteParticipant.chat_id == chat_id,
                RouletteParticipant.user_id == user_id,
            )
            .limit(1)
        )
        row = (await session.execute(stmt)).scalar_one_or_none()
        if row:
            return str(row).lstrip("@").lower()

        stmt2 = select(User.username).where(User.tg_id == user_id).limit(1)
        row2 = (await session.execute(stmt2)).scalar_one_or_none()
        if row2:
            return str(row2).lstrip("@").lower()
        return None

    async def _mention_stats(
        self,
        session: AsyncSession,
        *,
        chat_id: int,
        a: int,
        b: int,
    ) -> tuple[int, int]:
        """Return (cross_mention_count, denominator=A_total+B_total) over the 30d window."""
        from sqlalchemy import func, select

        from ..models import Message

        cutoff = datetime.utcnow() - timedelta(days=WINDOW_DAYS)

        totals_stmt = (
            select(Message.user_id, func.count().label("cnt"))
            .where(
                Message.chat_id == chat_id,
                Message.is_bot.is_(False),
                Message.date >= cutoff,
                Message.user_id.in_([a, b]),
            )
            .group_by(Message.user_id)
        )
        totals = dict((row.user_id, int(row.cnt)) for row in (await session.execute(totals_stmt)).all())
        denom = totals.get(a, 0) + totals.get(b, 0)

        name_a = await self._resolve_username(session, chat_id=chat_id, user_id=a)
        name_b = await self._resolve_username(session, chat_id=chat_id, user_id=b)

        count = 0
        if name_b:
            stmt = (
                select(func.count())
                .where(
                    Message.chat_id == chat_id,
                    Message.user_id == a,
                    Message.is_bot.is_(False),
                    Message.date >= cutoff,
                    func.lower(Message.text).contains(f"@{name_b}"),
                )
            )
            count += int((await session.execute(stmt)).scalar() or 0)
        if name_a:
            stmt = (
                select(func.count())
                .where(
                    Message.chat_id == chat_id,
                    Message.user_id == b,
                    Message.is_bot.is_(False),
                    Message.date >= cutoff,
                    func.lower(Message.text).contains(f"@{name_a}"),
                )
            )
            count += int((await session.execute(stmt)).scalar() or 0)

        return count, denom

    async def _co_active_days(
        self,
        session: AsyncSession,
        *,
        chat_id: int,
        a: int,
        b: int,
    ) -> int:
        """Return number of distinct calendar days within the 30d window where BOTH a and b posted."""
        from sqlalchemy import func, select

        from ..models import Message

        cutoff = datetime.utcnow() - timedelta(days=WINDOW_DAYS)
        day = func.date(Message.date)

        stmt = (
            select(day.label("day"), Message.user_id)
            .where(
                Message.chat_id == chat_id,
                Message.is_bot.is_(False),
                Message.date >= cutoff,
                Message.user_id.in_([a, b]),
            )
            .group_by(day, Message.user_id)
        )
        rows = (await session.execute(stmt)).all()
        days_a: set[str] = set()
        days_b: set[str] = set()
        for row in rows:
            key = str(row.day)
            if row.user_id == a:
                days_a.add(key)
            elif row.user_id == b:
                days_b.add(key)
        return len(days_a & days_b)
