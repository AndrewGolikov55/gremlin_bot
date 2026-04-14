from __future__ import annotations

import pytest
from unittest.mock import AsyncMock

from app.services.spontaneity import (
    ActionKind,
    InterjectTrigger,
    SpontaneityPolicy,
)


def _make_policy(
    *,
    now: float = 1_000_000.0,
    rng: float = 0.5,
    app_conf: dict | None = None,
    chat_conf: dict | None = None,
):
    redis = AsyncMock()
    redis.get = AsyncMock(return_value=None)
    redis.set = AsyncMock(return_value=True)

    app_config = AsyncMock()
    app_config.get_all = AsyncMock(return_value=app_conf or {})

    settings = AsyncMock()
    settings.get_all = AsyncMock(return_value=chat_conf or {})

    return SpontaneityPolicy(
        redis=redis,
        app_config=app_config,
        settings=settings,
        clock=lambda: now,
        rng=lambda: rng,
    )


@pytest.mark.asyncio
async def test_mark_acted_interject_sets_long_timer() -> None:
    policy = _make_policy(now=1_234_567.0)
    await policy.mark_acted(chat_id=-100, action=ActionKind.INTERJECT)
    policy._redis.set.assert_awaited_once_with(
        "spontaneity:long:-100",
        "1234567.0",
        ex=86400,
    )


@pytest.mark.asyncio
async def test_mark_acted_direct_reply_sets_long_timer() -> None:
    policy = _make_policy(now=1_234_567.0)
    await policy.mark_acted(chat_id=-100, action=ActionKind.DIRECT_REPLY)
    policy._redis.set.assert_awaited_once_with(
        "spontaneity:long:-100",
        "1234567.0",
        ex=86400,
    )


@pytest.mark.asyncio
async def test_mark_acted_reaction_sets_short_timer() -> None:
    policy = _make_policy(now=1_234_567.0)
    await policy.mark_acted(chat_id=-100, action=ActionKind.REACTION)
    policy._redis.set.assert_awaited_once_with(
        "spontaneity:short:-100",
        "1234567.0",
        ex=86400,
    )
