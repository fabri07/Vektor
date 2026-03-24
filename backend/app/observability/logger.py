"""
Structured logging via structlog.

Usage:
    from app.observability.logger import get_logger, bind_request_context
    logger = get_logger(__name__)
    logger.info("event.name", key="value")

Fields included in every log entry:
  timestamp, level, logger, environment (via contextvars)

Fields bound per-request (set in middleware + deps):
  tenant_id, user_id, endpoint, method, status_code, duration_ms, environment

Fields bound per-job:
  job_name, tenant_id, duration_ms, success/error
"""

import logging
import sys
import time
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any
from uuid import UUID

import structlog

from app.config.settings import get_settings


def configure_logging() -> None:
    """Initialize structlog with JSON output for production, pretty for dev."""
    settings = get_settings()

    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
    ]

    if settings.is_production:
        renderer: structlog.types.Processor = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=True)

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.handlers = [handler]
    root_logger.setLevel(logging.DEBUG if settings.DEBUG else logging.INFO)

    # Silence noisy third-party loggers
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.engine").setLevel(
        logging.INFO if settings.DEBUG else logging.WARNING
    )


# Configure on module import
configure_logging()


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)  # type: ignore[no-any-return]


def bind_request_context(
    *,
    tenant_id: UUID | str | None = None,
    user_id: UUID | str | None = None,
) -> None:
    """
    Bind tenant_id and user_id into the structlog contextvars for the current
    request. Call this from the auth dependency once the user is resolved.
    All subsequent log calls within the same request will include these fields.
    """
    ctx: dict[str, Any] = {}
    if tenant_id is not None:
        ctx["tenant_id"] = str(tenant_id)
    if user_id is not None:
        ctx["user_id"] = str(user_id)
    if ctx:
        structlog.contextvars.bind_contextvars(**ctx)


@contextmanager
def log_job(
    job_name: str,
    tenant_id: str | UUID | None = None,
    logger: structlog.stdlib.BoundLogger | None = None,  # type: ignore[assignment]
) -> Generator[structlog.stdlib.BoundLogger, None, None]:
    """
    Context manager that logs job start/end with duration_ms and success/error.

    Usage:
        with log_job("jobs.score_recalc", tenant_id=tid) as jlog:
            jlog.info("doing something")
    """
    _logger: structlog.stdlib.BoundLogger = logger or get_logger(job_name)
    bound = _logger.bind(
        job_name=job_name,
        **({"tenant_id": str(tenant_id)} if tenant_id else {}),
    )
    t0 = time.monotonic()
    bound.info("job.started")
    try:
        yield bound  # type: ignore[misc]
        duration_ms = int((time.monotonic() - t0) * 1000)
        bound.info("job.completed", duration_ms=duration_ms, success=True)
    except Exception as exc:
        duration_ms = int((time.monotonic() - t0) * 1000)
        bound.error(
            "job.failed",
            duration_ms=duration_ms,
            success=False,
            error=str(exc),
            exc_type=type(exc).__name__,
        )
        raise
