"""Redis connection for pub/sub fan-out."""

import os

import redis.asyncio as aioredis

_redis: aioredis.Redis | None = None


def get_redis() -> aioredis.Redis:
    if _redis is None:
        raise RuntimeError("Redis not initialized — call init_redis() first")
    return _redis


async def init_redis() -> None:
    global _redis
    url = os.environ.get("REDIS_URL", "redis://localhost:6379")
    _redis = aioredis.from_url(url, decode_responses=True)


async def close_redis() -> None:
    global _redis
    if _redis:
        await _redis.aclose()
        _redis = None
