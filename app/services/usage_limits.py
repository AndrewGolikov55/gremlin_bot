from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any, Protocol
from zoneinfo import ZoneInfo

UsageRequest = tuple[str, int]


class RedisPipeline(Protocol):
    def incr(self, key: str, amount: int = 1) -> "RedisPipeline": ...
    def decr(self, key: str, amount: int = 1) -> "RedisPipeline": ...
    def expire(self, key: str, ttl: int, nx: bool = False) -> "RedisPipeline": ...
    def set(self, key: str, value: Any) -> "RedisPipeline": ...
    def delete(self, *keys: str) -> "RedisPipeline": ...
    async def execute(self) -> list[Any]: ...


class UsageRedis(Protocol):
    def pipeline(self) -> RedisPipeline: ...
    async def get(self, key: str) -> Any: ...
    async def mget(self, *keys: str | list[str] | tuple[str, ...]) -> list[Any]: ...


class UsageLimiter:
    """Simple per-chat daily limiter backed by Redis."""

    def __init__(self, redis: UsageRedis, *, timezone: ZoneInfo | None = None) -> None:
        self._redis = redis
        self._tz = timezone or ZoneInfo("UTC")

    async def consume(
        self,
        chat_id: int,
        requests: Sequence[UsageRequest],
    ) -> tuple[bool, dict[str, int], list[str]]:
        """Try to consume usage units for the given chat.

        Parameters
        ----------
        chat_id: int
            Target chat identifier.
        requests: Sequence[UsageRequest]
            A sequence of pairs (prefix, limit) describing counters to increment.

        Returns
        -------
        allowed: bool
            False if any limit would be exceeded.
        counts: dict[str, int]
            Updated counts for successfully processed keys. When `allowed` is False the counts
            reflect the state prior to increment.
        exceeded: list[str]
            List of prefixes that would exceed their limit.
        """

        valid = [
            (prefix, self._key(prefix, chat_id), limit)
            for prefix, limit in requests
            if limit and limit > 0
        ]
        if not valid:
            return True, {}, []

        pipe = self._redis.pipeline()
        for _, key, _ in valid:
            pipe.incr(key, 1)
        increments = await pipe.execute()

        exceeded: list[str] = []
        for (prefix, _key, limit), value in zip(valid, increments):
            if value > limit:
                exceeded.append(prefix)

        if exceeded:
            pipe = self._redis.pipeline()
            for (_, key, limit), value in zip(valid, increments):
                if limit > 0 and value > 0:
                    pipe.decr(key, 1)
            await pipe.execute()

            current_values = await self._redis.mget([key for _, key, _ in valid])
            counts = {
                prefix: int(value or 0)
                for (prefix, _, _), value in zip(valid, current_values)
            }
            return False, counts, exceeded

        ttl = self._seconds_left()
        pipe = self._redis.pipeline()
        for _, key, limit in valid:
            if limit > 0:
                pipe.expire(key, ttl, nx=False)
        await pipe.execute()

        counts = {prefix: value for (prefix, _, _), value in zip(valid, increments)}
        return True, counts, []

    async def get_usage(self, chat_id: int, prefix: str) -> int:
        key = self._key(prefix, chat_id)
        value = await self._redis.get(key)
        return int(value or 0)

    async def refund(self, chat_id: int, prefixes: Sequence[str]) -> None:
        if not prefixes:
            return
        keys = [self._key(prefix, chat_id) for prefix in prefixes]
        existing_values = await self._redis.mget(keys)
        pipe = self._redis.pipeline()
        for key in keys:
            pipe.decr(key, 1)
        results = await pipe.execute()
        corrections = []
        for key, before, result in zip(keys, existing_values, results):
            if result is None or result >= 0:
                continue
            if before is None:
                corrections.append(("delete", key))
            else:
                corrections.append(("set", key))
        if corrections:
            pipe = self._redis.pipeline()
            for action, key in corrections:
                if action == "delete":
                    pipe.delete(key)
                else:
                    pipe.set(key, 0)
            await pipe.execute()

    def _key(self, prefix: str, chat_id: int) -> str:
        day = datetime.now(self._tz).strftime("%Y%m%d")
        return f"usage:{prefix}:{chat_id}:{day}"

    def _seconds_left(self) -> int:
        now = datetime.now(self._tz)
        tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        return max(1, int((tomorrow - now).total_seconds()))
