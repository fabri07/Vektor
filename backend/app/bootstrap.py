"""
Dependency initialization and teardown.

Called during FastAPI lifespan: startup() on boot, shutdown() on exit.
"""

from app.observability.logger import get_logger

logger = get_logger(__name__)


async def startup() -> None:
    """Initialize all application dependencies (fail-closed)."""
    print("[bootstrap] startup begin", flush=True)
    await _init_database()
    print("[bootstrap] database ready", flush=True)
    await _init_redis()
    print("[bootstrap] redis ready", flush=True)

    logger.info("bootstrap.startup.complete")
    print("[bootstrap] startup complete", flush=True)


async def shutdown() -> None:
    """Gracefully release all application resources."""
    await _close_database()
    await _close_redis()
    logger.info("bootstrap.shutdown.complete")


# ── Database ──────────────────────────────────────────────────────────────────

async def _init_database() -> None:
    # Validate connectivity on startup (fail fast)
    from sqlalchemy import text  # noqa: PLC0415

    from app.persistence.db.engine import engine  # noqa: PLC0415

    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))
    logger.info("bootstrap.database.connected")


async def _close_database() -> None:
    from app.persistence.db.engine import engine  # noqa: PLC0415

    await engine.dispose()
    logger.info("bootstrap.database.disconnected")


# ── Redis ─────────────────────────────────────────────────────────────────────

async def _init_redis() -> None:
    import asyncio  # noqa: PLC0415

    from app.config.settings import get_settings  # noqa: PLC0415
    from app.persistence.db.redis import get_redis_pool  # noqa: PLC0415

    s = get_settings()
    # Log the resolved URL host (without password) so we can see what Railway injected
    safe_url = s.REDIS_URL.split("@")[-1] if "@" in s.REDIS_URL else s.REDIS_URL
    print(f"[bootstrap] connecting to redis: {safe_url}", flush=True)

    pool = await get_redis_pool()
    try:
        await asyncio.wait_for(pool.ping(), timeout=5.0)
    except TimeoutError as exc:
        print(f"[bootstrap] redis ping TIMEOUT after 5s — URL={safe_url}", flush=True)
        raise RuntimeError(f"Redis unreachable: {safe_url}") from exc
    except Exception as exc:
        print(f"[bootstrap] redis ping FAILED: {type(exc).__name__}: {exc}", flush=True)
        raise
    logger.info("bootstrap.redis.connected")


async def _close_redis() -> None:
    from app.persistence.db.redis import close_redis_pool  # noqa: PLC0415

    await close_redis_pool()
    logger.info("bootstrap.redis.disconnected")
