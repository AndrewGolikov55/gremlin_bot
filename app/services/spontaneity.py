"""Policy service that decides whether the bot should speak or react.

Owns the "should the bot act now?" decision that is currently scattered
across :mod:`app.services.interjector` and :mod:`app.services.reactions`.
This module defines the skeleton: enums, the class shell with dependency
injection, and :meth:`SpontaneityPolicy.mark_acted` — the write path that
records cooldown timestamps in Redis. The probability / cooldown read
paths (:meth:`can_interject`, :meth:`can_react`) will be filled in by
follow-up tasks.
"""

from __future__ import annotations

import logging
import random
import time
from datetime import datetime
from datetime import time as dtime
from enum import Enum
from typing import Any, Callable

from redis.asyncio import Redis

from .app_config import AppConfigService
from .settings import SettingsService

logger = logging.getLogger(__name__)

_LONG_KEY = "spontaneity:long:{chat_id}"
_SHORT_KEY = "spontaneity:short:{chat_id}"
# 24h TTL is well above any realistic cooldown; keys are refreshed on every
# action so this is just a safety net against stale entries lingering forever.
_KEY_TTL_SEC = 24 * 60 * 60

_DEFAULT_INTERJECT_P = 5
_DEFAULT_REVIVE_P = 50
_DEFAULT_REACTION_P = 5
_DEFAULT_INTERJECT_COOLDOWN_MIN = 30
_DEFAULT_REACT_COOLDOWN_MIN = 10


def _parse_quiet_hours(raw: object) -> tuple[dtime, dtime] | None:
    """Parse a ``"HH:MM-HH:MM"`` string into a pair of :class:`datetime.time`."""

    if not isinstance(raw, str) or "-" not in raw:
        return None
    try:
        start_s, end_s = raw.split("-", 1)
        start = datetime.strptime(start_s.strip(), "%H:%M").time()
        end = datetime.strptime(end_s.strip(), "%H:%M").time()
        return start, end
    except (ValueError, AttributeError):
        return None


def _is_quiet_now(window: tuple[dtime, dtime] | None, now: datetime) -> bool:
    """Return ``True`` if ``now``'s wall-clock time falls inside ``window``."""

    if window is None:
        return False
    start, end = window
    current = now.time()
    if start <= end:
        return start <= current <= end
    # Window crosses midnight (e.g. 22:00-06:00).
    return current >= start or current <= end


class InterjectTrigger(Enum):
    """Why we are considering an unsolicited message right now."""

    NEW_MESSAGE = "new_message"
    REVIVE = "revive"


class ActionKind(Enum):
    """What the bot just did — determines which cooldown timer to bump."""

    INTERJECT = "interject"
    DIRECT_REPLY = "direct_reply"
    REACTION = "reaction"


class SpontaneityPolicy:
    """Central authority on bot spontaneity.

    Reads probabilities and cooldowns from :class:`AppConfigService` /
    :class:`SettingsService`, tracks last-action timestamps in Redis, and
    answers "can I speak?" / "can I react?" questions. ``clock`` and
    ``rng`` are injected so tests can pin time and randomness.
    """

    def __init__(
        self,
        *,
        redis: Redis,
        app_config: AppConfigService,
        settings: SettingsService,
        clock: Callable[[], float] = time.time,
        rng: Callable[[], float] = random.random,
    ) -> None:
        self._redis = redis
        self._app_config = app_config
        self._settings = settings
        self._clock = clock
        self._rng = rng

    async def mark_acted(self, *, chat_id: int, action: ActionKind) -> None:
        """Record that the bot just performed ``action`` in ``chat_id``.

        Messages (``INTERJECT`` / ``DIRECT_REPLY``) share the "long"
        cooldown timer; reactions use a separate "short" timer so they
        don't lock out messages and vice versa.
        """

        now = self._clock()
        if action in (ActionKind.INTERJECT, ActionKind.DIRECT_REPLY):
            key = _LONG_KEY.format(chat_id=chat_id)
        elif action is ActionKind.REACTION:
            key = _SHORT_KEY.format(chat_id=chat_id)
        else:
            raise ValueError(f"unknown action: {action}")
        await self._redis.set(key, str(now), ex=_KEY_TTL_SEC)

    async def can_interject(self, chat_id: int, *, trigger: InterjectTrigger) -> bool:
        """Decide whether the bot may send an unsolicited message now.

        Vetoes in this order: quiet hours (per-chat), the "long" cooldown
        shared with direct replies, then a dice roll against either
        ``interject_p`` or ``revive_p`` depending on the trigger.
        """

        app_conf = await self._app_config.get_all()
        chat_conf = await self._settings.get_all(chat_id)

        if self._is_quiet(chat_conf):
            return False

        cooldown_min = int(
            app_conf.get("interject_cooldown_min", _DEFAULT_INTERJECT_COOLDOWN_MIN) or 0
        )
        if await self._long_cooldown_active(chat_id, cooldown_min):
            return False

        if trigger is InterjectTrigger.REVIVE:
            probability = int(app_conf.get("revive_p", _DEFAULT_REVIVE_P) or 0)
        else:
            probability = int(app_conf.get("interject_p", _DEFAULT_INTERJECT_P) or 0)

        return self._roll_dice(probability)

    async def can_react(self, chat_id: int) -> bool:
        raise NotImplementedError

    def _roll_dice(self, probability_percent: int) -> bool:
        if probability_percent <= 0:
            return False
        if probability_percent >= 100:
            return True
        return self._rng() * 100 < probability_percent

    async def _long_cooldown_active(self, chat_id: int, cooldown_min: int) -> bool:
        if cooldown_min <= 0:
            return False
        raw = await self._redis.get(_LONG_KEY.format(chat_id=chat_id))
        if raw is None:
            return False
        try:
            last = float(raw.decode() if isinstance(raw, (bytes, bytearray)) else raw)
        except (ValueError, AttributeError):
            return False
        return (self._clock() - last) < cooldown_min * 60

    def _is_quiet(self, chat_conf: dict[str, Any]) -> bool:
        window = _parse_quiet_hours(chat_conf.get("quiet_hours"))
        return _is_quiet_now(window, datetime.now())
