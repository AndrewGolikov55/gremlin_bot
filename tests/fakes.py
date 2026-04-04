from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any


class FakePipeline:
    def __init__(self, redis: "FakeRedis") -> None:
        self._redis = redis
        self._commands: list[Callable[[], Awaitable[Any]]] = []

    def set(self, key: str, value: Any, ex: int | None = None) -> "FakePipeline":
        self._commands.append(lambda: self._redis.set(key, value, ex=ex))
        return self

    def get(self, key: str) -> "FakePipeline":
        self._commands.append(lambda: self._redis.get(key))
        return self

    def delete(self, *keys: str) -> "FakePipeline":
        self._commands.append(lambda: self._redis.delete(*keys))
        return self

    def incr(self, key: str, amount: int = 1) -> "FakePipeline":
        self._commands.append(lambda: self._redis.incr(key, amount))
        return self

    def decr(self, key: str, amount: int = 1) -> "FakePipeline":
        self._commands.append(lambda: self._redis.decr(key, amount))
        return self

    def mget(self, *keys: str) -> "FakePipeline":
        self._commands.append(lambda: self._redis.mget(*keys))
        return self

    def expire(self, key: str, seconds: int, nx: bool = False) -> "FakePipeline":
        self._commands.append(lambda: self._redis.expire(key, seconds, nx=nx))
        return self

    async def execute(self) -> list[Any]:
        results: list[Any] = []
        for command in self._commands:
            results.append(await command())
        self._commands.clear()
        return results


class FakeRedis:
    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        self._expires_at: dict[str, float] = {}

    def pipeline(self) -> FakePipeline:
        return FakePipeline(self)

    async def get(self, key: str) -> Any:
        self._purge_if_expired(key)
        return self._data.get(key)

    async def set(self, key: str, value: Any, ex: int | None = None) -> str:
        self._data[key] = value
        if ex is None:
            self._expires_at.pop(key, None)
        else:
            self._expires_at[key] = time.monotonic() + ex
        return "OK"

    async def delete(self, *keys: str) -> int:
        deleted = 0
        for key in keys:
            self._purge_if_expired(key)
            if key in self._data:
                deleted += 1
                self._data.pop(key, None)
                self._expires_at.pop(key, None)
        return deleted

    async def incr(self, key: str, amount: int = 1) -> int:
        return await self._change_integer(key, amount)

    async def decr(self, key: str, amount: int = 1) -> int:
        return await self._change_integer(key, -amount)

    async def mget(self, *keys: str | list[str] | tuple[str, ...]) -> list[Any]:
        resolved_keys: tuple[str, ...]
        if len(keys) == 1 and isinstance(keys[0], (list, tuple)):
            resolved_keys = tuple(keys[0])
        else:
            resolved_keys = tuple(key for key in keys if isinstance(key, str))
        return [await self.get(key) for key in resolved_keys]

    async def expire(self, key: str, seconds: int, nx: bool = False) -> bool:
        self._purge_if_expired(key)
        if key not in self._data:
            return False

        if nx and key in self._expires_at:
            return False

        if seconds <= 0:
            await self.delete(key)
            return True

        self._expires_at[key] = time.monotonic() + seconds
        return True

    def _purge_if_expired(self, key: str) -> None:
        expires_at = self._expires_at.get(key)
        if expires_at is not None and expires_at <= time.monotonic():
            self._data.pop(key, None)
            self._expires_at.pop(key, None)

    async def _change_integer(self, key: str, delta: int) -> int:
        self._purge_if_expired(key)
        current = self._data.get(key, 0)
        if isinstance(current, str):
            current = int(current)
        next_value = int(current) + delta
        self._data[key] = next_value
        return next_value
