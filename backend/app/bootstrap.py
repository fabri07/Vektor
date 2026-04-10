"""
Dependency initialization and teardown.

Called during FastAPI lifespan: startup() on boot, shutdown() on exit.
"""

from app.observability.logger import get_logger

logger = get_logger(__name__)


async def startup() -> None:
    """Initialize all application dependencies (fail-closed)."""
    await _init_database()
    await _init_redis()

    logger.info("bootstrap.startup.complete")


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
    from app.persistence.db.redis import get_redis_pool  # noqa: PLC0415

    pool = await get_redis_pool()
    await pool.ping()
    logger.info("bootstrap.redis.connected")


async def _close_redis() -> None:
    from app.persistence.db.redis import close_redis_pool  # noqa: PLC0415

    await close_redis_pool()
    logger.info("bootstrap.redis.disconnected")
