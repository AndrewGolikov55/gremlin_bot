from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

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
    """Return today's Moscow midnight as a naive UTC datetime suitable for comparison with stored Message.date."""
    aware = now if now.tzinfo else now.replace(tzinfo=MoscowTZ)
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
