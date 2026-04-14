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
from enum import Enum
from typing import Callable

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
        raise NotImplementedError

    async def can_react(self, chat_id: int) -> bool:
        raise NotImplementedError
