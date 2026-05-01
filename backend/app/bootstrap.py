"""
Dependency initialization and teardown.

Called during FastAPI lifespan: startup() on boot, shutdown() on exit.
"""

from app.observability.logger import get_logger

logger = get_logger(__name__)


async def startup() -> None:
    """Initialize application dependencies without blocking liveness."""
    from app.config.settings import get_settings  # noqa: PLC0415
    _settings = get_settings()

    try:
        await _init_database()
    except Exception as exc:
        if _settings.ENVIRONMENT == "production":
            raise RuntimeError(
                f"Database no disponible en producción: {exc}"
            ) from exc
        logger.warning(
            "bootstrap.database.unavailable",
            exc_type=type(exc).__name__,
            exc_msg=str(exc),
        )

    try:
        await _init_redis()
    except Exception as exc:
        logger.warning(
            "bootstrap.redis.unavailable",
            exc_type=type(exc).__name__,
            exc_msg=str(exc),
        )

    _check_celery_broker()

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


# ── Celery ────────────────────────────────────────────────────────────────────

def _check_celery_broker() -> None:
    """Verifica que el broker de Celery sea alcanzable. No-op si Celery está deshabilitado."""
    from app.config.settings import get_settings  # noqa: PLC0415

    s = get_settings()
    if not s.ENABLE_SCORE_RECALCULATION:
        return

    try:
        from app.jobs.celery_app import celery_app  # noqa: PLC0415

        conn = celery_app.connection_for_read()
        conn.ensure_connection(max_retries=1, timeout=3)
        conn.release()
        logger.info("bootstrap.celery.connected")
    except Exception as exc:
        logger.warning(
            "bootstrap.celery.unavailable",
            exc_type=type(exc).__name__,
            exc_msg=str(exc),
            detail="El score de salud async no funcionará hasta que Redis/Celery esté disponible.",
        )
