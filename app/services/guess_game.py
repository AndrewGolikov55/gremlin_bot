from __future__ import annotations

import json
import logging
import random
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Awaitable, Callable
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..models.message import Message

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
    selection_mode: str  # "llm" | "random_fallback"


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
        app_config: object,
        *,
        bot: object = None,
        display_name: Callable[[int, int], Awaitable[str]] | None = None,
        llm_pick: Callable[..., Awaitable[LLMPick | None]] | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self.sessionmaker = sessionmaker
        self.app_config = app_config
        self.bot = bot
        self._display_name = display_name or self._default_display_name
        self._llm_pick = llm_pick or self._llm_pick_default
        self._rng = rng or random.Random()

    async def _default_display_name(self, chat_id: int, user_id: int) -> str:
        # Real impl in Task 10 hits TG via self.bot. For unit tests, replace via ctor.
        return f"user{user_id}"

    async def _llm_pick_default(
        self,
        author_messages: dict[int, list[Message]],
        *,
        chat_id: int,
    ) -> LLMPick | None:
        # Real impl in Task 8 calls llm_generate. Default falls through to random.
        return None

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

        llm_choice = await self._llm_pick(author_messages, chat_id=chat_id)
        selection_mode = "llm"
        chosen_author: int | None = None
        chosen_message: Message | None = None

        if llm_choice is not None and llm_choice.author_user_id in valid_authors:
            for m in author_messages[llm_choice.author_user_id]:
                if m.message_id == llm_choice.message_id:
                    name = await self._display_name(chat_id, llm_choice.author_user_id)
                    if not text_contains_author_identity(m.text, username=None, first_name=name):
                        chosen_author = llm_choice.author_user_id
                        chosen_message = m
                    break

        if chosen_message is None:
            selection_mode = "random_fallback"
            shuffled = list(author_messages.keys())
            self._rng.shuffle(shuffled)
            chosen_author = shuffled[0]
            chosen_message = self._rng.choice(author_messages[chosen_author])

        assert chosen_author is not None and chosen_message is not None

        # Build options
        other_authors = [uid for uid in author_messages.keys() if uid != chosen_author]
        self._rng.shuffle(other_authors)
        n_decoys = min(MAX_OPTIONS - 1, len(other_authors))
        decoys = other_authors[:n_decoys]
        options = decoys + [chosen_author]
        self._rng.shuffle(options)
        labels = [await self._display_name(chat_id, uid) for uid in options]
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
