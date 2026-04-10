"""
Véktor API — FastAPI application factory.

Entry point: uvicorn app.main:app
"""

import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from slowapi.util import get_remote_address

from app.application.middleware.tenant import TenantMiddleware
from app.bootstrap import shutdown, startup
from app.config.settings import get_settings
from app.observability.logger import get_logger

logger = get_logger(__name__)
settings = get_settings()

# ── Rate limiter (shared instance, imported by routers) ───────────────────────

limiter = Limiter(key_func=get_remote_address, default_limits=["200/minute"])


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Manage application startup and shutdown lifecycle."""
    print(f"[lifespan] entering — env={settings.ENVIRONMENT}", flush=True)
    logger.info("vektor.startup", environment=settings.ENVIRONMENT)
    await startup()
    print("[lifespan] startup done, yielding to app", flush=True)
    yield
    await shutdown()
    logger.info("vektor.shutdown")


def create_app() -> FastAPI:
    """Application factory — instantiates and configures FastAPI."""
    app = FastAPI(
        title="Véktor API",
        version="1.0.0",
        description="Plataforma SaaS de salud financiera para PYMEs argentinas.",
        docs_url="/docs" if not settings.is_production else None,
        redoc_url="/redoc" if not settings.is_production else None,
        openapi_url="/openapi.json" if not settings.is_production else None,
        lifespan=lifespan,
    )

    # ── Rate limiter state ────────────────────────────────────────────────────
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)  # type: ignore[arg-type]
    app.add_middleware(SlowAPIMiddleware)

    # ── Tenant context (RLS) ──────────────────────────────────────────────────
    app.add_middleware(TenantMiddleware)

    # ── CORS ─────────────────────────────────────────────────────────────────
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "Accept", "X-Request-ID"],
    )

    # ── Security headers ──────────────────────────────────────────────────────
    @app.middleware("http")
    async def security_headers(request: Request, call_next):  # type: ignore[no-untyped-def]
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; frame-ancestors 'none'"
        )
        if settings.is_production:
            response.headers["Strict-Transport-Security"] = (
                "max-age=31536000; includeSubDomains"
            )
        return response

    # ── Request logging ───────────────────────────────────────────────────────
    @app.middleware("http")
    async def request_logger(request: Request, call_next):  # type: ignore[no-untyped-def]
        import structlog.contextvars  # noqa: PLC0415

        # Reset per-request context and pre-bind known fields
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(
            environment=settings.ENVIRONMENT,
            method=request.method,
            endpoint=request.url.path,
        )

        t0 = time.monotonic()
        response = await call_next(request)
        duration_ms = int((time.monotonic() - t0) * 1000)

        logger.info(
            "http.request",
            status_code=response.status_code,
            duration_ms=duration_ms,
        )
        return response

    # ── Routers ───────────────────────────────────────────────────────────────
    from app.api.v1.router import api_router  # noqa: PLC0415

    app.include_router(api_router, prefix="/api/v1")

    # ── Exception handlers ────────────────────────────────────────────────────
    @app.exception_handler(ValidationError)
    async def pydantic_validation_handler(
        request: Request, exc: ValidationError
    ) -> JSONResponse:
        logger.warning(
            "request.validation_error",
            path=str(request.url.path),
            errors=exc.errors(),
        )
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={"detail": exc.errors()},
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(
        request: Request, exc: Exception
    ) -> JSONResponse:
        logger.error(
            "request.unhandled_exception",
            path=str(request.url.path),
            exc_type=type(exc).__name__,
            exc_msg=str(exc),
        )
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"detail": f"[DEBUG] {type(exc).__name__}: {exc}"},
        )

    # ── Health check ──────────────────────────────────────────────────────────
    @app.get("/health", tags=["Infra"], summary="Health check")
    async def health_check() -> dict[str, str]:
        return {"status": "ok", "version": "1.0.0", "environment": settings.ENVIRONMENT}

    @app.get("/ready", tags=["Infra"], summary="Readiness check")
    async def readiness_check() -> JSONResponse:
        checks = {
            "database": await _check_database_ready(),
            "redis": await _check_redis_ready(),
        }
        ready = all(check["ok"] for check in checks.values())
        return JSONResponse(
            status_code=status.HTTP_200_OK if ready else status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "status": "ready" if ready else "degraded",
                "version": "1.0.0",
                "environment": settings.ENVIRONMENT,
                "checks": checks,
            },
        )

    return app


async def _check_database_ready() -> dict[str, Any]:
    try:
        from sqlalchemy import text  # noqa: PLC0415

        from app.persistence.db.engine import engine  # noqa: PLC0415

        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return {"ok": True}
    except Exception as exc:
        return {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
        }


async def _check_redis_ready() -> dict[str, Any]:
    try:
        from app.persistence.db.redis import get_redis_pool  # noqa: PLC0415

        redis = await get_redis_pool()
        await redis.ping()
        return {"ok": True}
    except Exception as exc:
        return {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
        }


app = create_app()
