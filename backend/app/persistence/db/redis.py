"""
Async Redis connection pool.
"""

from redis.asyncio import Redis
from redis.asyncio.connection import ConnectionPool

from app.config.settings import get_settings

settings = get_settings()

_pool: ConnectionPool | None = None


async def get_redis_pool() -> Redis:
    global _pool  # noqa: PLW0603
    if _pool is None:
        _pool = ConnectionPool.from_url(
            settings.REDIS_URL,
            max_connections=20,
            decode_responses=True,
            socket_connect_timeout=5,
            socket_timeout=5,
        )
    return Redis(connection_pool=_pool)


async def close_redis_pool() -> None:
    global _pool  # noqa: PLW0603
    if _pool is not None:
        await _pool.aclose()
        _pool = None


async def get_redis() -> Redis:
    """FastAPI dependency for Redis client."""
    return await get_redis_pool()
