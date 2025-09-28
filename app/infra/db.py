import os

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

def init_engine_and_sessionmaker():
    url = os.getenv("DATABASE_URL", "postgresql+asyncpg://bot:bot@db:5432/botdb")
    engine: AsyncEngine = create_async_engine(url, echo=False, future=True)
    sessionmaker: async_sessionmaker[AsyncSession] = async_sessionmaker(engine, expire_on_commit=False)
    return engine, sessionmaker


async def shutdown_engine(engine: AsyncEngine) -> None:
    await engine.dispose()
