from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from aiogram import Bot
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..utils.text import strip_markdown
from .app_config import AppConfigService
from .llm.client import LLMError, LLMRateLimitError, resolve_llm_options
from .llm.client import generate as llm_generate
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

    @staticmethod
    def _fallback_body(*, metrics: ShipMetrics) -> str:
        keywords = ", ".join(metrics.pref_overlap_keywords) if metrics.pref_overlap_keywords else "не нашлось"
        return (
            f"Общие интересы: {keywords}. "
            f"Друг другу отвечают: {metrics.reply_count} раз."
        )

    @staticmethod
    def _build_header(*, name_a: str, name_b: str, score: int) -> str:
        return f"💞 {name_a} ↔ {name_b} — совместимость {score}/100"

    @staticmethod
    def _assemble_message(*, header: str, body: str) -> str:
        body = body.strip()
        if not body:
            return header
        return f"{header}\n\n{body}"

    def _build_user_prompt(
        self,
        *,
        name_a: str,
        name_b: str,
        score: int,
        metrics: ShipMetrics,
    ) -> str:
        intersect = ", ".join(metrics.pref_overlap_keywords) if metrics.pref_overlap_keywords else "почти нет"
        return (
            f"Шипперинг пары: {name_a} и {name_b}.\n"
            f"Совместимость уже посчитана и будет выведена шапкой ВЫШЕ твоего ответа — её повторять НЕ нужно.\n"
            f"Твоё дело: дать 2-3 строки острой аналитики пары в своей персоне.\n"
            f"\n"
            f"Сырые метрики для опоры:\n"
            f"- Совместимость: {score}/100\n"
            f"- Друг другу отвечают: {metrics.reply_count} раз\n"
            f"- Упоминают друг друга: {metrics.mention_count} раз\n"
            f"- Совместные активные дни: {metrics.co_active_days}/{WINDOW_DAYS}\n"
            f"- Общие интересы из профилей: {intersect}\n"
            f"\n"
            f"Правила:\n"
            f"- Опирайся на ЦИФРЫ выше и реальные пересечения, не выдумывай факты.\n"
            f"- Не повторяй имена в шапку — это уже сделано.\n"
            f"- Не пиши число совместимости — оно уже в шапке.\n"
            f"- Никакого markdown — ни **, ни *, ни __, ни _, ни backticks. Только plain text.\n"
            f"- Не сюсюкай, маты по делу можно.\n"
            f"- Только тело, 2-3 коротких строки. Никаких приветствий, никаких пояснений."
        )

    async def _render(
        self,
        *,
        chat_id: int,
        name_a: str,
        name_b: str,
        score: int,
        metrics: ShipMetrics,
    ) -> str:
        conf = await self.settings.get_all(chat_id)
        style = str(conf.get("style", "standup"))
        system_prompt = await self.personas.get(style)
        user_prompt = self._build_user_prompt(
            name_a=name_a, name_b=name_b, score=score, metrics=metrics
        )

        app_conf = await self.app_config.get_all()
        provider = resolve_llm_options(app_conf)
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        try:
            text = await llm_generate(
                messages,
                temperature=LLM_TEMPERATURE,
                max_tokens=LLM_MAX_TOKENS,
                provider=provider,
            )
        except (LLMError, LLMRateLimitError):
            logger.exception("ship: LLM provider failed chat=%s", chat_id)
            return self._fallback_body(metrics=metrics)
        except Exception:
            logger.exception("ship: unexpected LLM error chat=%s", chat_id)
            return self._fallback_body(metrics=metrics)

        if not text or not text.strip():
            return self._fallback_body(metrics=metrics)
        return strip_markdown(text).strip()

    async def resolve_candidate(
        self,
        *,
        chat_id: int,
        candidate: tuple[str, int] | tuple[str, str],
    ) -> tuple[int, str] | None:
        """Convert a parsed handler candidate into (user_id, display_name).

        candidate is one of:
        - ("id", <user_id>) — already known (e.g. from text_mention entity)
        - ("username", "@alice" | "alice")
        Returns None iff candidate cannot be resolved in this chat.
        """
        from sqlalchemy import func, select

        from ..models import RouletteParticipant, User

        kind, value = candidate
        async with self.sessionmaker() as session:
            if kind == "id":
                user_id = int(value)
                stmt = (
                    select(RouletteParticipant.username)
                    .where(
                        RouletteParticipant.chat_id == chat_id,
                        RouletteParticipant.user_id == user_id,
                    )
                    .limit(1)
                )
                name = (await session.execute(stmt)).scalar_one_or_none()
                if name:
                    return user_id, str(name)
                stmt2 = select(User.username).where(User.tg_id == user_id).limit(1)
                name2 = (await session.execute(stmt2)).scalar_one_or_none()
                if name2:
                    return user_id, str(name2)
                return user_id, f"id{user_id}"

            if kind == "username":
                needle = str(value).lstrip("@").lower()
                if not needle:
                    return None
                u_stmt = (
                    select(RouletteParticipant.user_id, RouletteParticipant.username)
                    .where(
                        RouletteParticipant.chat_id == chat_id,
                        func.lower(RouletteParticipant.username) == needle,
                    )
                    .limit(1)
                )
                row = (await session.execute(u_stmt)).first()
                if row is not None:
                    return int(row.user_id), str(row.username)
                u_stmt2 = (
                    select(User.tg_id, User.username)
                    .where(func.lower(User.username) == needle)
                    .limit(1)
                )
                row2 = (await session.execute(u_stmt2)).first()
                if row2 is not None:
                    return int(row2.tg_id), str(row2.username)
                return None

            return None

    async def _has_any_messages(
        self,
        session: AsyncSession,
        *,
        chat_id: int,
        user_id: int,
    ) -> bool:
        from sqlalchemy import func, select

        from ..models import Message

        cutoff = datetime.utcnow() - timedelta(days=WINDOW_DAYS)
        stmt = (
            select(func.count())
            .where(
                Message.chat_id == chat_id,
                Message.user_id == user_id,
                Message.is_bot.is_(False),
                Message.date >= cutoff,
            )
        )
        return int((await session.execute(stmt)).scalar() or 0) > 0

    async def compute_or_cached(
        self,
        *,
        chat_id: int,
        a: tuple[int, str],
        b: tuple[int, str],
        bot_id: int,
    ) -> ShipOutcome:
        """Main entry point. Returns ShipOutcome.

        a, b: tuples of (user_id, display_name).
        bot_id: bot's own Telegram user id (to refuse bot-in-pair).
        score=-1 indicates a refusal/meta result that the handler should render as-is.
        """
        a_id, a_name = a
        b_id, b_name = b

        if a_id == b_id:
            return ShipOutcome(
                score=-1,
                rendered_text=(
                    f"💞 {a_name} сам с собой — совместимость 100%, "
                    f"но это не диагноз, а синдром."
                ),
                cached=False,
            )

        if a_id == bot_id or b_id == bot_id:
            return ShipOutcome(
                score=-1,
                rendered_text="С ботом нельзя, мне больно.",
                cached=False,
            )

        async with self._get_lock(chat_id):
            async with self.sessionmaker() as session:
                has_a = await self._has_any_messages(session, chat_id=chat_id, user_id=a_id)
                has_b = await self._has_any_messages(session, chat_id=chat_id, user_id=b_id)
            if not has_a or not has_b:
                missing = a_name if not has_a else b_name
                return ShipOutcome(
                    score=-1,
                    rendered_text=f"Не из чего считать совместимость для @{missing}.",
                    cached=False,
                )

            ua, ub = self.canonicalize_pair(a_id, b_id)
            async with self.sessionmaker() as session:
                cached = await self._load_cached(session, chat_id=chat_id, a=ua, b=ub)
            if cached is not None:
                return ShipOutcome(
                    score=cached.score,
                    rendered_text=cached.rendered_text,
                    cached=True,
                )

            async with self.sessionmaker() as session:
                metrics = await self._compute_metrics(session, chat_id=chat_id, a=a_id, b=b_id)
            score = self.aggregate_score(metrics)
            body = await self._render(
                chat_id=chat_id,
                name_a=a_name,
                name_b=b_name,
                score=score,
                metrics=metrics,
            )
            header = self._build_header(name_a=a_name, name_b=b_name, score=score)
            rendered = self._assemble_message(header=header, body=body)
            payload = {
                "reply_count": metrics.reply_count,
                "mention_count": metrics.mention_count,
                "co_active_days": metrics.co_active_days,
                "pref_overlap_keywords": metrics.pref_overlap_keywords,
                "reply_rate": metrics.reply_rate,
                "mention_rate": metrics.mention_rate,
                "co_activity": metrics.co_activity,
                "pref_overlap": metrics.pref_overlap,
            }
            await self._persist(
                chat_id=chat_id, a=ua, b=ub,
                score=score, payload=payload, rendered_text=rendered,
            )
            return ShipOutcome(score=score, rendered_text=rendered, cached=False)

    async def pick_random_pair(
        self,
        *,
        chat_id: int,
        bot_id: int,
    ) -> tuple[tuple[int, str], tuple[int, str]] | None:
        """Return two random active users (excluding bot) for /games ship-random.

        Prefers pairs that don't have a fresh (<24h) cached ship_result.
        Returns None if fewer than 2 active human authors in the last 30 days.
        """
        from sqlalchemy import select

        from ..models import Message
        from ..models import ShipResult as ShipResultModel

        cutoff = datetime.utcnow() - timedelta(days=WINDOW_DAYS)
        async with self.sessionmaker() as session:
            stmt = (
                select(Message.user_id)
                .where(
                    Message.chat_id == chat_id,
                    Message.is_bot.is_(False),
                    Message.user_id != bot_id,
                    Message.date >= cutoff,
                )
                .group_by(Message.user_id)
            )
            user_ids = sorted({int(r[0]) for r in (await session.execute(stmt)).all()})
        if len(user_ids) < 2:
            return None

        # All unordered pairs
        all_pairs: list[tuple[int, int]] = [
            (user_ids[i], user_ids[j])
            for i in range(len(user_ids))
            for j in range(i + 1, len(user_ids))
        ]

        # Filter pairs by cache state (prefer uncached/stale)
        cache_cutoff = datetime.utcnow() - CACHE_TTL
        async with self.sessionmaker() as session:
            cached_stmt = select(ShipResultModel.user_id_a, ShipResultModel.user_id_b).where(
                ShipResultModel.chat_id == chat_id,
                ShipResultModel.computed_at >= cache_cutoff,
            )
            cached_set = {(int(r[0]), int(r[1])) for r in (await session.execute(cached_stmt)).all()}

        uncached = [p for p in all_pairs if p not in cached_set]
        pool = uncached if uncached else all_pairs
        chosen = random.choice(pool)

        a_res = await self.resolve_candidate(chat_id=chat_id, candidate=("id", chosen[0]))
        b_res = await self.resolve_candidate(chat_id=chat_id, candidate=("id", chosen[1]))
        # resolve_candidate("id", *) never returns None — always falls back to id<n>
        assert a_res is not None and b_res is not None
        return a_res, b_res
