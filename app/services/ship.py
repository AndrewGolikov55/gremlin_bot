from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from typing import TYPE_CHECKING

from aiogram import Bot
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .app_config import AppConfigService
from .persona import StylePromptService
from .settings import SettingsService

if TYPE_CHECKING:
    from ..models import ShipResult

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

    async def _pref_overlap(
        self,
        session: AsyncSession,
        *,
        chat_id: int,
        a: int,
        b: int,
    ) -> tuple[list[str], float]:
        """Return (sorted intersection keywords, ratio = |A∩B| / max(1, |A∪B|)).

        Combines preferences + projects + identity lists from UserMemoryProfile.
        Case-insensitive, deduplicated within each user.
        """
        from sqlalchemy import select

        from ..models import UserMemoryProfile

        def _bag(profile: UserMemoryProfile | None) -> set[str]:
            if profile is None:
                return set()
            items: list[str] = []
            items.extend(profile.preferences or [])
            items.extend(profile.projects or [])
            items.extend(profile.identity or [])
            return {str(x).strip().lower() for x in items if str(x).strip()}

        stmt = select(UserMemoryProfile).where(
            UserMemoryProfile.chat_id == chat_id,
            UserMemoryProfile.user_id.in_([a, b]),
        )
        profiles = {p.user_id: p for p in (await session.execute(stmt)).scalars().all()}
        bag_a = _bag(profiles.get(a))
        bag_b = _bag(profiles.get(b))
        intersection = bag_a & bag_b
        union = bag_a | bag_b
        if not union:
            return [], 0.0
        return sorted(intersection), len(intersection) / len(union)

    async def _compute_metrics(
        self,
        session: AsyncSession,
        *,
        chat_id: int,
        a: int,
        b: int,
    ) -> ShipMetrics:
        reply_count, reply_denom = await self._reply_stats(session, chat_id=chat_id, a=a, b=b)
        mention_count, mention_denom = await self._mention_stats(session, chat_id=chat_id, a=a, b=b)
        co_days = await self._co_active_days(session, chat_id=chat_id, a=a, b=b)
        keywords, pref_ratio = await self._pref_overlap(session, chat_id=chat_id, a=a, b=b)

        reply_rate = min(1.0, reply_count / reply_denom) if reply_denom > 0 else 0.0
        mention_rate = min(1.0, mention_count / mention_denom) if mention_denom > 0 else 0.0
        co_activity = min(1.0, co_days / WINDOW_DAYS)

        return ShipMetrics(
            reply_count=reply_count,
            mention_count=mention_count,
            co_active_days=co_days,
            pref_overlap_keywords=keywords,
            reply_rate=reply_rate,
            mention_rate=mention_rate,
            co_activity=co_activity,
            pref_overlap=pref_ratio,
        )

    @staticmethod
    def aggregate_score(metrics: ShipMetrics) -> int:
        weighted = (
            WEIGHT_REPLY * metrics.reply_rate
            + WEIGHT_MENTION * metrics.mention_rate
            + WEIGHT_COACTIVITY * metrics.co_activity
            + WEIGHT_PREF * metrics.pref_overlap
        )
        return max(0, min(100, round(100 * weighted)))

    async def _load_cached(
        self,
        session: AsyncSession,
        *,
        chat_id: int,
        a: int,
        b: int,
    ) -> ShipResult | None:
        """Return cached ShipResult for pair (must be canonicalized) iff computed_at within 24h."""
        from sqlalchemy import select

        from ..models import ShipResult as ShipResultModel

        cutoff = datetime.utcnow() - CACHE_TTL
        stmt = (
            select(ShipResultModel)
            .where(
                ShipResultModel.chat_id == chat_id,
                ShipResultModel.user_id_a == a,
                ShipResultModel.user_id_b == b,
                ShipResultModel.computed_at >= cutoff,
            )
            .limit(1)
        )
        return (await session.execute(stmt)).scalar_one_or_none()

    async def _persist(
        self,
        *,
        chat_id: int,
        a: int,
        b: int,
        score: int,
        payload: dict,
        rendered_text: str,
    ) -> None:
        """Upsert ShipResult row for the canonicalized pair."""
        from sqlalchemy import select
        from sqlalchemy.exc import IntegrityError

        from ..models import ShipResult as ShipResultModel

        async with self.sessionmaker() as session:
            stmt = select(ShipResultModel).where(
                ShipResultModel.chat_id == chat_id,
                ShipResultModel.user_id_a == a,
                ShipResultModel.user_id_b == b,
            )
            existing = (await session.execute(stmt)).scalar_one_or_none()
            now = datetime.utcnow()
            if existing is None:
                session.add(ShipResultModel(
                    chat_id=chat_id,
                    user_id_a=a,
                    user_id_b=b,
                    score=score,
                    payload=payload,
                    rendered_text=rendered_text,
                    computed_at=now,
                ))
            else:
                existing.score = score
                existing.payload = payload
                existing.rendered_text = rendered_text
                existing.computed_at = now
            try:
                await session.commit()
            except IntegrityError:
                # Race: another worker inserted concurrently — that's fine, we keep theirs.
                await session.rollback()
                logger.info(
                    "ship: integrity race on persist chat=%s pair=(%s,%s)",
                    chat_id, a, b,
                )
