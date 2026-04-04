from __future__ import annotations

import pytest_asyncio

from tests.db import create_test_sessionmaker
from tests.fakes import FakeRedis


@pytest_asyncio.fixture
async def sessionmaker():
    sessionmaker = await create_test_sessionmaker()
    try:
        yield sessionmaker
    finally:
        engine = getattr(sessionmaker, "_engine", None)
        if engine is not None:
            await engine.dispose()


@pytest_asyncio.fixture
async def fake_redis() -> FakeRedis:
    return FakeRedis()
