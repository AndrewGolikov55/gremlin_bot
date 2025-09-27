import os

from redis.asyncio import Redis


def init_redis() -> Redis:
    url = os.getenv("REDIS_URL", "redis://redis:6379/0")
    return Redis.from_url(url)


async def shutdown_redis(redis: Redis) -> None:
    await redis.close()

