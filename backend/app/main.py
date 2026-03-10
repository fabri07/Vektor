"""
Véktor API — FastAPI application factory.

Entry point: uvicorn app.main:app
"""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from app.bootstrap import shutdown, startup
from app.config.settings import get_settings
from app.observability.logger import get_logger

logger = get_logger(__name__)
settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Manage application startup and shutdown lifecycle."""
    logger.info("vektor.startup", environment=settings.ENVIRONMENT)
    await startup()
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

    # ── Middleware ────────────────────────────────────────────────────────────
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

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
            content={"detail": "Internal server error"},
        )

    # ── Health check ──────────────────────────────────────────────────────────
    @app.get("/health", tags=["Infra"], summary="Health check")
    async def health_check() -> dict[str, str]:
        return {"status": "ok", "version": "1.0.0", "environment": settings.ENVIRONMENT}

    return app


app = create_app()
