from __future__ import annotations

from zoneinfo import ZoneInfo

from app.services.usage_limits import UsageLimiter
from tests.fakes import FakeRedis


async def test_consume_sets_ttl_for_successful_consumption(fake_redis: FakeRedis) -> None:
    limiter = UsageLimiter(fake_redis, timezone=ZoneInfo("Europe/Moscow"))

    allowed, counts, exceeded = await limiter.consume(42, [("summary", 2)])

    assert allowed is True
    assert counts == {"summary": 1}
    assert exceeded == []
    key = limiter._key("summary", 42)
    assert key in fake_redis._expires_at
    assert await limiter.get_usage(42, "summary") == 1


async def test_consume_rolls_back_when_any_limit_is_exceeded(fake_redis: FakeRedis) -> None:
    limiter = UsageLimiter(fake_redis, timezone=ZoneInfo("Europe/Moscow"))

    assert await limiter.consume(42, [("summary", 1)])
    allowed, counts, exceeded = await limiter.consume(42, [("summary", 1), ("llm", 2)])

    assert allowed is False
    assert counts == {"summary": 1, "llm": 0}
    assert exceeded == ["summary"]
    assert await limiter.get_usage(42, "summary") == 1
    assert await limiter.get_usage(42, "llm") == 0


async def test_refund_never_leaves_negative_counter(fake_redis: FakeRedis) -> None:
    limiter = UsageLimiter(fake_redis)

    await limiter.refund(42, ["summary"])

    assert await limiter.get_usage(42, "summary") == 0

    await limiter.consume(42, [("summary", 1)])
    await limiter.refund(42, ["summary"])

    assert await limiter.get_usage(42, "summary") == 0


async def test_refund_missing_key_does_not_leave_zero_key(fake_redis: FakeRedis) -> None:
    limiter = UsageLimiter(fake_redis, timezone=ZoneInfo("Europe/Moscow"))

    key = limiter._key("summary", 42)
    await limiter.refund(42, ["summary"])

    assert await fake_redis.get(key) is None
