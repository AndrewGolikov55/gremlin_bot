from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

logger = logging.getLogger("bot.dice_game")

MoscowTZ = ZoneInfo("Europe/Moscow")


def _moscow_midnight(now: datetime) -> datetime:
    """Today's Moscow midnight as a naive UTC datetime.

    Naive `now` is interpreted as UTC (matching `datetime.utcnow()`).
    """
    aware = now if now.tzinfo else now.replace(tzinfo=ZoneInfo("UTC"))
    msk = aware.astimezone(MoscowTZ)
    midnight_msk = msk.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight_msk.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)


def compute_delta(picks: list[int], dice_value: int) -> int:
    """Return roulette score delta for a dice roll outcome.

    -2 if single pick wins (1/6), -1 if double pick wins (2/6), else 0.
    """
    if dice_value not in picks:
        return 0
    return -2 if len(picks) == 1 else -1


class AlreadyPlayedTodayError(Exception):
    """Raised when a user tries to roll twice in the same Moscow day."""
