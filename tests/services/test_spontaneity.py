from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.services.spontaneity import (
    ActionKind,
    SpontaneityPolicy,
)


def _make_policy(
    *,
    now: float = 1_000_000.0,
    rng: float = 0.5,
    app_conf: dict | None = None,
    chat_conf: dict | None = None,
) -> SpontaneityPolicy:
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
    policy._redis.set.assert_awaited_once_with(  # type: ignore[attr-defined]
        "spontaneity:long:-100",
        "1234567.0",
        ex=86400,
    )


@pytest.mark.asyncio
async def test_mark_acted_direct_reply_sets_long_timer() -> None:
    policy = _make_policy(now=1_234_567.0)
    await policy.mark_acted(chat_id=-100, action=ActionKind.DIRECT_REPLY)
    policy._redis.set.assert_awaited_once_with(  # type: ignore[attr-defined]
        "spontaneity:long:-100",
        "1234567.0",
        ex=86400,
    )


@pytest.mark.asyncio
async def test_mark_acted_reaction_sets_short_timer() -> None:
    policy = _make_policy(now=1_234_567.0)
    await policy.mark_acted(chat_id=-100, action=ActionKind.REACTION)
    policy._redis.set.assert_awaited_once_with(  # type: ignore[attr-defined]
        "spontaneity:short:-100",
        "1234567.0",
        ex=86400,
    )
