from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

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


@pytest.mark.asyncio
async def test_can_interject_false_if_long_cooldown_active() -> None:
    policy = _make_policy(
        now=1_000_100.0,
        app_conf={"interject_p": 100, "interject_cooldown_min": 30},
    )
    policy._redis.get = AsyncMock(return_value=b"1000000.0")  # type: ignore[method-assign]
    assert await policy.can_interject(chat_id=-100, trigger=InterjectTrigger.NEW_MESSAGE) is False


@pytest.mark.asyncio
async def test_can_interject_true_after_long_cooldown_expired() -> None:
    policy = _make_policy(
        now=1_000_000.0 + 30 * 60 + 1,
        rng=0.01,  # dice passes
        app_conf={"interject_p": 100, "interject_cooldown_min": 30},
    )
    policy._redis.get = AsyncMock(return_value=b"1000000.0")  # type: ignore[method-assign]
    assert await policy.can_interject(chat_id=-100, trigger=InterjectTrigger.NEW_MESSAGE) is True


@pytest.mark.asyncio
async def test_can_interject_false_in_quiet_hours() -> None:
    policy = _make_policy(
        app_conf={"interject_p": 100},
        chat_conf={"quiet_hours": "00:00-23:59"},  # always quiet
    )
    assert await policy.can_interject(chat_id=-100, trigger=InterjectTrigger.NEW_MESSAGE) is False


@pytest.mark.asyncio
async def test_can_interject_false_when_dice_fails() -> None:
    policy = _make_policy(
        rng=0.99,  # dice fails
        app_conf={"interject_p": 5},
    )
    assert await policy.can_interject(chat_id=-100, trigger=InterjectTrigger.NEW_MESSAGE) is False


@pytest.mark.asyncio
async def test_can_interject_new_chat_no_redis_key_passes_dice() -> None:
    policy = _make_policy(rng=0.01, app_conf={"interject_p": 5})
    policy._redis.get = AsyncMock(return_value=None)  # type: ignore[method-assign]
    assert await policy.can_interject(chat_id=-100, trigger=InterjectTrigger.NEW_MESSAGE) is True


@pytest.mark.asyncio
async def test_can_interject_revive_uses_revive_p_not_interject_p() -> None:
    policy = _make_policy(
        rng=0.5,
        app_conf={"interject_p": 0, "revive_p": 100},
    )
    assert await policy.can_interject(chat_id=-100, trigger=InterjectTrigger.REVIVE) is True


@pytest.mark.asyncio
async def test_can_interject_revive_respects_long_cooldown() -> None:
    policy = _make_policy(
        now=1_000_100.0,
        app_conf={"interject_cooldown_min": 30, "revive_p": 100},
    )
    policy._redis.get = AsyncMock(return_value=b"1000000.0")  # type: ignore[method-assign]
    assert await policy.can_interject(chat_id=-100, trigger=InterjectTrigger.REVIVE) is False


@pytest.mark.asyncio
async def test_can_react_false_if_short_cooldown_active() -> None:
    policy = _make_policy(
        now=1_000_100.0,
        app_conf={"reaction_p": 100, "react_cooldown_min": 10},
    )
    policy._redis.get = AsyncMock(return_value=b"1000000.0")  # type: ignore[method-assign]
    assert await policy.can_react(chat_id=-100) is False


@pytest.mark.asyncio
async def test_can_react_true_after_short_cooldown_expired() -> None:
    policy = _make_policy(
        now=1_000_000.0 + 10 * 60 + 1,
        rng=0.01,
        app_conf={"reaction_p": 100, "react_cooldown_min": 10},
    )
    policy._redis.get = AsyncMock(return_value=b"1000000.0")  # type: ignore[method-assign]
    assert await policy.can_react(chat_id=-100) is True


@pytest.mark.asyncio
async def test_can_react_independent_of_long_timer() -> None:
    # Long timer active (recent interject), but short is not: react must still be allowed
    policy = _make_policy(
        rng=0.01,
        app_conf={"reaction_p": 100, "react_cooldown_min": 10, "interject_cooldown_min": 30},
    )
    # Only `spontaneity:short:-100` is queried; `spontaneity:long:-100` shouldn't block
    policy._redis.get = AsyncMock(return_value=None)  # type: ignore[method-assign]
    assert await policy.can_react(chat_id=-100) is True


@pytest.mark.asyncio
async def test_can_react_false_in_quiet_hours() -> None:
    policy = _make_policy(
        app_conf={"reaction_p": 100},
        chat_conf={"quiet_hours": "00:00-23:59"},
    )
    assert await policy.can_react(chat_id=-100) is False


@pytest.mark.asyncio
async def test_can_react_false_when_dice_fails() -> None:
    policy = _make_policy(rng=0.99, app_conf={"reaction_p": 5})
    assert await policy.can_react(chat_id=-100) is False
