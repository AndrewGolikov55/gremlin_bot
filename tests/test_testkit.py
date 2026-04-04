from __future__ import annotations

from typing import cast

from sqlalchemy import text
from sqlalchemy.dialects.sqlite import dialect as sqlite_dialect
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.schema import CreateTable
from sqlalchemy.sql.schema import Table

from app.models import AppSetting, ChatSetting, UserMemoryProfile
from tests.fakes import FakeRedis


async def test_fake_redis_roundtrip(fake_redis: FakeRedis) -> None:
    redis = fake_redis

    assert await redis.get("missing") is None

    await redis.set("alpha", "beta")
    assert await redis.get("alpha") == "beta"

    await redis.delete("alpha")
    assert await redis.get("alpha") is None

    await redis.incr("counter")
    await redis.incr("counter")
    await redis.decr("counter")
    assert await redis.get("counter") == 1

    await redis.set("one", "1")
    await redis.set("two", "2")
    assert await redis.mget("one", "two", "three") == ["1", "2", None]
    assert await redis.mget(["one", "two", "three"]) == ["1", "2", None]

    await redis.set("ttl", "value", ex=10)
    assert await redis.get("ttl") == "value"

    pipeline = redis.pipeline()
    pipeline.set("pipe", "value", ex=10)
    pipeline.incr("counter")
    pipeline.expire("pipe", 5, nx=True)
    assert await pipeline.execute() == ["OK", 2, False]
    assert await redis.get("pipe") == "value"
    assert await redis.get("counter") == 2


def test_json_columns_compile_for_sqlite() -> None:
    app_setting_sql = str(
        CreateTable(cast(Table, AppSetting.__table__)).compile(dialect=sqlite_dialect())
    )
    chat_setting_sql = str(
        CreateTable(cast(Table, ChatSetting.__table__)).compile(dialect=sqlite_dialect())
    )

    assert "JSON" in app_setting_sql
    assert "JSON" in chat_setting_sql


async def test_json_columns_exist_in_sqlite(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    async with sessionmaker() as session:
        app_columns = list((await session.execute(text("PRAGMA table_info('app_settings')"))).all())
        chat_columns = list((await session.execute(text("PRAGMA table_info('chat_settings')"))).all())

    assert "value" in [row[1] for row in app_columns]
    assert "value" in [row[1] for row in chat_columns]


async def test_user_memory_profile_json_round_trip(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with sessionmaker() as session:
        session.add(
            UserMemoryProfile(
                chat_id=1,
                user_id=2,
                identity=["builder"],
                preferences=["minimal"],
                boundaries=["nope"],
                projects=["gremlin"],
            )
        )
        await session.commit()

        profile = await session.get(UserMemoryProfile, (1, 2))

    assert profile is not None
    assert profile.identity == ["builder"]
    assert profile.preferences == ["minimal"]
    assert profile.boundaries == ["nope"]
    assert profile.projects == ["gremlin"]
