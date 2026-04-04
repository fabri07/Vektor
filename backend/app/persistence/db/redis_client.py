"""Redis async client — dependency injectable para FastAPI."""

from collections.abc import AsyncGenerator

from redis.asyncio import Redis

from app.config.settings import get_settings


async def get_redis() -> AsyncGenerator[Redis, None]:
    """FastAPI dependency: yields a Redis connection, closes on exit."""
    settings = get_settings()
    r: Redis = Redis.from_url(settings.REDIS_URL, decode_responses=True)
    try:
        yield r
    finally:
        await r.aclose()
