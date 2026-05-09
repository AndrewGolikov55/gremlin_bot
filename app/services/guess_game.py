from __future__ import annotations

import json
import logging
import random
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable, Literal
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..models.guess_round import GuessRound
from ..models.message import Message
from ..models.roulette import RouletteScoreAdjustment
from .llm.client import LLMError, LLMRateLimitError, resolve_llm_options
from .llm.client import generate as llm_generate

logger = logging.getLogger("bot.guess_game")

MIN_LEN = 30
MAX_LEN = 500
WINDOW_DAYS = 30
MIN_ELIGIBLE_PER_AUTHOR = 5

_FORBIDDEN_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"https?://", re.IGNORECASE),
    re.compile(r"\bt\.me/", re.IGNORECASE),
    re.compile(r"@\w+"),
)

MoscowTZ = ZoneInfo("Europe/Moscow")

LLM_SYSTEM = (
    "Ты — помощник для игры «Угадай кто сказал». На вход дают список авторов и"
    " их сообщений. Выбери ОДНО самое характерное/смешное/кринжовое сообщение,"
    " по которому игроки легко узнают автора. Ответ строго JSON, без markdown,"
    " формат: {\"author_user_id\": <int>, \"message_id\": <int>, \"reason\": \"...\"}."
)


def _moscow_midnight(now: datetime) -> datetime:
    """Return today's Moscow midnight as a naive UTC datetime suitable for comparison with stored Message.date.

    Naive `now` is interpreted as UTC (matching `datetime.utcnow()`).
    """
    aware = now if now.tzinfo else now.replace(tzinfo=ZoneInfo("UTC"))
    msk = aware.astimezone(MoscowTZ)
    midnight_msk = msk.replace(hour=0, minute=0, second=0, microsecond=0)
    # Stored Message.date is naive UTC; convert Moscow midnight to UTC and drop tz.
    return midnight_msk.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)


def _passes_text_filters(text: str | None) -> bool:
    if not text:
        return False
    if len(text) < MIN_LEN or len(text) > MAX_LEN:
        return False
    if text.startswith("/"):
        return False
    for pat in _FORBIDDEN_PATTERNS:
        if pat.search(text):
            return False
    return True


async def pick_messages_for_author(
    session: AsyncSession,
    chat_id: int,
    user_id: int,
    *,
    now: datetime,
    limit: int = 6,
) -> list[Message]:
    cutoff_old = now - timedelta(days=WINDOW_DAYS)
    cutoff_today = _moscow_midnight(now)
    stmt = (
        select(Message)
        .where(
            Message.chat_id == chat_id,
            Message.user_id == user_id,
            Message.is_bot.is_(False),
            Message.tg_file_id.is_(None),
            Message.media_group_id.is_(None),
            Message.date >= cutoff_old,
            Message.date < cutoff_today,
            func.length(Message.text) >= MIN_LEN,
            func.length(Message.text) <= MAX_LEN,
        )
        .order_by(Message.date.desc())
    )
    rows = (await session.execute(stmt)).scalars().all()
    eligible = [m for m in rows if _passes_text_filters(m.text)]
    return eligible[:limit]


async def pick_candidate_authors(
    session: AsyncSession,
    chat_id: int,
    *,
    now: datetime,
    limit: int = 8,
) -> list[int]:
    """Return user_ids of authors with ≥5 eligible messages in the last 30d, ordered by message count."""
    cutoff_old = now - timedelta(days=WINDOW_DAYS)
    cutoff_today = _moscow_midnight(now)
    stmt = (
        select(Message.user_id, Message.text)
        .where(
            Message.chat_id == chat_id,
            Message.is_bot.is_(False),
            Message.tg_file_id.is_(None),
            Message.media_group_id.is_(None),
            Message.date >= cutoff_old,
            Message.date < cutoff_today,
            func.length(Message.text) >= MIN_LEN,
            func.length(Message.text) <= MAX_LEN,
        )
    )
    rows = (await session.execute(stmt)).all()
    counts: dict[int, int] = {}
    for user_id, text in rows:
        if _passes_text_filters(text):
            counts[user_id] = counts.get(user_id, 0) + 1
    authors = [uid for uid, n in counts.items() if n >= MIN_ELIGIBLE_PER_AUTHOR]
    authors.sort(key=lambda uid: counts[uid], reverse=True)
    return authors[:limit]


@dataclass(frozen=True)
class LLMPick:
    author_user_id: int
    message_id: int
    reason: str | None = None


def parse_llm_pick(
    raw: str,
    *,
    valid_authors: set[int],
    valid_message_ids: set[int],
) -> LLMPick | None:
    """Tolerantly parse a JSON object from LLM output (handles plain JSON or fenced code blocks)."""
    if not raw:
        return None
    text = raw.strip()
    if text.startswith("```"):
        # Strip leading fence with optional language tag, then trailing fence
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = text.rstrip().removesuffix("```").rstrip()
    try:
        obj = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(obj, dict):
        return None
    try:
        author_id = int(obj["author_user_id"])
        message_id = int(obj["message_id"])
    except (KeyError, TypeError, ValueError):
        return None
    if author_id not in valid_authors or message_id not in valid_message_ids:
        return None
    reason = obj.get("reason")
    if not isinstance(reason, str):
        reason = None
    return LLMPick(author_user_id=author_id, message_id=message_id, reason=reason)


def text_contains_author_identity(
    text: str,
    *,
    username: str | None,
    first_name: str | None,
) -> bool:
    """Detect whether the message text leaks the author's identity (case-insensitive substring)."""
    haystack = text.lower()
    needles: list[str] = []
    if username:
        needles.append(username.lower())
    if first_name:
        chunks = first_name.strip().split()
        if chunks:
            chunk = chunks[0].lower()
            if len(chunk) >= 3:
                needles.append(chunk)
    return any(n in haystack for n in needles if n)


class NoCandidatesError(Exception):
    """Raised when there are not enough eligible authors to start a round."""


@dataclass(frozen=True)
class PreparedRound:
    chat_id: int
    author_user_id: int
    source_message_id: int
    text: str
    option_user_ids: list[int]
    option_labels: list[str]
    correct_option_id: int
    selection_mode: Literal["llm", "random_fallback"]


MAX_OPTIONS = 4
MIN_OPTIONS = 2
TG_QUESTION_LIMIT = 290


def _truncate_question(text: str) -> str:
    if len(text) <= TG_QUESTION_LIMIT:
        return text
    return text[: TG_QUESTION_LIMIT - 1].rstrip() + "…"


class GuessGameService:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        app_config: Any,
        *,
        bot: Any = None,
        display_name: Callable[[int, int], Awaitable[str]] | None = None,
        display_user: Callable[[int, int], Awaitable[tuple[str | None, str | None]]] | None = None,
        llm_pick: Callable[..., Awaitable[LLMPick | None]] | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self.sessionmaker = sessionmaker
        self.app_config = app_config
        self.bot = bot
        self._display_name = display_name or self._default_display_name
        self._display_user = display_user or self._default_display_user
        self._llm_pick = llm_pick or self._llm_pick_real
        self._rng = rng or random.Random()

    async def _default_display_name(self, chat_id: int, user_id: int) -> str:
        if self.bot is None:
            return f"user{user_id}"
        try:
            member = await self.bot.get_chat_member(chat_id, user_id)
        except Exception:  # noqa: BLE001 — TG API failure → harmless display fallback
            return f"user{user_id}"
        user = member.user
        if user.full_name:
            return user.full_name
        if user.username:
            return f"@{user.username}"
        return f"user{user_id}"

    async def _default_display_user(self, chat_id: int, user_id: int) -> tuple[str | None, str | None]:
        """Return (username, first_name) for the post-filter. Falls back to (None, None)."""
        if self.bot is None:
            return None, None
        try:
            member = await self.bot.get_chat_member(chat_id, user_id)
        except Exception:  # noqa: BLE001
            return None, None
        return member.user.username, member.user.first_name

    async def _llm_pick_real(
        self,
        author_messages: dict[int, list[Message]],
        *,
        chat_id: int,
    ) -> LLMPick | None:
        valid_authors = set(author_messages.keys())
        valid_message_ids = {m.message_id for msgs in author_messages.values() for m in msgs}
        if not valid_authors:
            return None

        candidates_payload = [
            {
                "user_id": uid,
                "messages": [{"id": m.message_id, "text": m.text} for m in msgs],
            }
            for uid, msgs in author_messages.items()
        ]
        user_payload = json.dumps(
            {"task": "Выбери самое характерное сообщение", "candidates": candidates_payload},
            ensure_ascii=False,
        )

        conf = await self.app_config.get_all()
        provider = resolve_llm_options(conf)

        try:
            raw = await llm_generate(
                [
                    {"role": "system", "content": LLM_SYSTEM},
                    {"role": "user", "content": user_payload},
                ],
                temperature=0.7,
                max_tokens=200,
                provider=provider,
            )
        except (LLMError, LLMRateLimitError):
            logger.exception("guess.llm_pick failed for chat=%s", chat_id)
            return None
        except Exception:
            logger.exception("guess.llm_pick unexpected error for chat=%s", chat_id)
            return None

        return parse_llm_pick(raw, valid_authors=valid_authors, valid_message_ids=valid_message_ids)

    async def prepare_round(self, chat_id: int, *, now: datetime) -> PreparedRound:
        async with self.sessionmaker() as session:
            authors = await pick_candidate_authors(session, chat_id, now=now)
            if len(authors) < MIN_OPTIONS:
                raise NoCandidatesError(f"only {len(authors)} eligible authors")

            author_messages: dict[int, list[Message]] = {}
            for uid in authors:
                msgs = await pick_messages_for_author(session, chat_id, uid, now=now)
                if msgs:
                    author_messages[uid] = msgs

        if len(author_messages) < MIN_OPTIONS:
            raise NoCandidatesError("not enough authors with messages")

        valid_authors = set(author_messages.keys())

        display_cache: dict[int, str] = {}

        async def _named(uid: int) -> str:
            cached = display_cache.get(uid)
            if cached is not None:
                return cached
            name = await self._display_name(chat_id, uid)
            display_cache[uid] = name
            return name

        llm_choice = await self._llm_pick(author_messages, chat_id=chat_id)
        selection_mode: Literal["llm", "random_fallback"] = "random_fallback"
        chosen_author: int | None = None
        chosen_message: Message | None = None

        if llm_choice is not None and llm_choice.author_user_id in valid_authors:
            for m in author_messages[llm_choice.author_user_id]:
                if m.message_id == llm_choice.message_id:
                    username, first_name = await self._display_user(chat_id, llm_choice.author_user_id)
                    if not text_contains_author_identity(m.text, username=username, first_name=first_name):
                        chosen_author = llm_choice.author_user_id
                        chosen_message = m
                        selection_mode = "llm"
                    break

        if chosen_message is None:
            shuffled = list(author_messages.keys())
            self._rng.shuffle(shuffled)
            chosen_author = shuffled[0]
            chosen_message = self._rng.choice(author_messages[chosen_author])

        if chosen_author is None or chosen_message is None:
            raise RuntimeError("guess_game.prepare_round invariant violated: no message chosen")

        # Build options
        other_authors = [uid for uid in author_messages.keys() if uid != chosen_author]
        self._rng.shuffle(other_authors)
        n_decoys = min(MAX_OPTIONS - 1, len(other_authors))
        decoys = other_authors[:n_decoys]
        options = decoys + [chosen_author]
        self._rng.shuffle(options)
        labels = [await _named(uid) for uid in options]
        correct_idx = options.index(chosen_author)

        return PreparedRound(
            chat_id=chat_id,
            author_user_id=chosen_author,
            source_message_id=chosen_message.message_id,
            text=_truncate_question(chosen_message.text),
            option_user_ids=options,
            option_labels=labels,
            correct_option_id=correct_idx,
            selection_mode=selection_mode,
        )

    async def can_start_today(self, *, chat_id: int, now: datetime) -> bool:
        midnight = _moscow_midnight(now)
        async with self.sessionmaker() as session:
            existing = (await session.execute(
                select(GuessRound.id)
                .where(GuessRound.chat_id == chat_id, GuessRound.started_at >= midnight)
                .limit(1)
            )).scalar_one_or_none()
        return existing is None

    async def persist_round(
        self,
        prepared: PreparedRound,
        *,
        poll_id: str,
        chat_message_id: int,
    ) -> int:
        async with self.sessionmaker() as session:
            row = GuessRound(
                chat_id=prepared.chat_id,
                poll_id=poll_id,
                chat_message_id=chat_message_id,
                source_chat_id=prepared.chat_id,
                source_message_id=prepared.source_message_id,
                author_user_id=prepared.author_user_id,
                correct_option_id=prepared.correct_option_id,
                option_user_ids=list(prepared.option_user_ids),
                started_at=datetime.utcnow(),
                selection_mode=prepared.selection_mode,
            )
            session.add(row)
            await session.commit()
            return row.id

    async def find_round_by_poll(self, poll_id: str) -> GuessRound | None:
        async with self.sessionmaker() as session:
            return (await session.execute(
                select(GuessRound).where(GuessRound.poll_id == poll_id)
            )).scalar_one_or_none()

    async def record_first_winner(self, *, round_id: int, user_id: int, now: datetime) -> bool:
        """Atomically claim the first-winner slot for a round. Returns True iff this user was the first."""
        async with self.sessionmaker() as session:
            async with session.begin():
                row = (await session.execute(
                    select(GuessRound).where(GuessRound.id == round_id).with_for_update()
                )).scalar_one_or_none()
                if row is None or row.first_winner_user_id is not None:
                    return False
                row.first_winner_user_id = user_id
                row.first_winner_at = now
                session.add(RouletteScoreAdjustment(
                    chat_id=row.chat_id,
                    user_id=user_id,
                    delta=-1,
                    reason="guess_first_winner",
                    source_id=round_id,
                ))
            return True
