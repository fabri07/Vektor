"""
Véktor API — FastAPI application factory.

Entry point: uvicorn app.main:app
"""

import time
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

import sentry_sdk
from fastapi import FastAPI, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError, ResponseValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from pydantic import ValidationError
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from app.api.v1.deps import rate_limit_key, subscription_http_error
from app.application.middleware.tenant import TenantMiddleware
from app.application.middleware.unhandled_errors import UnhandledErrorMiddleware
from app.application.services.respaldo_service import (
    VentaSinCompraError,
    VentaSinProductoError,
)
from app.application.services.stock_service import (
    InsufficientStockError,
    SaleProductNotFoundError,
)
from app.bootstrap import shutdown, startup
from app.config.settings import get_settings
from app.domain.pos_terminal import OFFLINE_HEADER, TERMINAL_HEADER
from app.domain.subscription import SUBSCRIPTION_ERRORS
from app.observability.logger import get_logger
from app.observability.sentry import init_sentry

logger = get_logger(__name__)
settings = get_settings()
init_sentry("web")

#: Lo único de un error de validación que va al log: dónde y por qué. Nunca
#: `input` (el valor rechazado: una contraseña, un PIN) ni `ctx` (puede traer la
#: excepción con el valor adentro).
_CLAVES_DE_ERROR_LOGUEABLES = ("loc", "msg", "type")


def errores_para_log(errores: Any) -> list[dict[str, Any]]:
    """Los errores de Pydantic sin los valores que mandó el cliente."""
    return [
        {k: e[k] for k in _CLAVES_DE_ERROR_LOGUEABLES if k in e}
        for e in errores
        if isinstance(e, dict)
    ]

# ── Rate limiter (shared instance, imported by routers) ───────────────────────

# `key_func=rate_limit_key` y no `get_remote_address`: es la MISMA resolución de
# IP que usa el `ip_hash` de los formularios públicos (ver `deps.client_ip`).
# Con `get_remote_address` —que ignora los headers del proxy— detrás del edge de
# Railway todos los visitantes podían compartir cubeta, y el `5/hour` del único
# embudo de alta pasaba a ser un techo GLOBAL. Ojo al cambiarlo: este limiter lo
# consumen TODOS los endpoints vía `default_limits`, no solo los públicos.
limiter = Limiter(key_func=rate_limit_key, default_limits=["200/minute"])

#: Headers que el front manda y que el preflight tiene que aceptar. Un header
#: propio que falte acá no da error en el servidor: el NAVEGADOR corta el
#: preflight con 400 y el request nunca sale. Así quedó la cola del POS
#: (`X-POS-Offline`) y toda caja habilitada (`X-POS-Terminal`) hasta 2026-10:
#: cada reintento parecía un corte de red. Header nuevo del front ⇒ va acá
#: (``test_todo_header_propio_del_front_esta_permitido`` lo exige leyendo el front).
#: ``Idempotency-Key`` faltó desde que el front lo empezó a mandar (2026-06): la
#: carga manual, los remitos y el capital nunca llegaban y quedaban «sin conexión»
#: en la cola del navegador (encontrado al verificar M·2a, 2026-10-11).
CORS_ALLOW_HEADERS: list[str] = [
    "Authorization",
    "Content-Type",
    "Accept",
    "X-Request-ID",
    "X-Trace-Id",
    "Idempotency-Key",
    "sentry-trace",
    "baggage",
    TERMINAL_HEADER,
    OFFLINE_HEADER,
]
CORS_EXPOSE_HEADERS: list[str] = ["X-Request-ID", "X-Trace-Id"]


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


async def _respuesta_500(request: Request, exc: Exception) -> Response:
    """El 500 de una excepción no manejada: log, Sentry y un cuerpo sin detalles."""
    # Una respuesta que no valida trae en su `str()` el `input` de cada error: el
    # dict entero de la respuesta, con `access_token`/`refresh_token` si es la del
    # login. Al log va dónde y por qué, como en los 422.
    exc_msg: object = (
        errores_para_log(exc.errors())
        if isinstance(exc, ResponseValidationError)
        else str(exc)
    )
    logger.error(
        "request.unhandled_exception",
        path=str(request.url.path),
        exc_type=type(exc).__name__,
        exc_msg=exc_msg,
    )
    # Este handler INTERCEPTA la excepción (nunca se repropaga), así que el
    # middleware automático de FastApiIntegration nunca la ve — sin esta
    # línea, Sentry queda con la integración activa pero cero eventos
    # reales de error.
    sentry_sdk.capture_exception(exc)
    detail = (
        f"[DEBUG] {type(exc).__name__}: {exc}"
        if settings.is_development or settings.DEBUG
        else "Internal Server Error"
    )
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": detail},
    )


def create_app() -> FastAPI:
    """Application factory — instantiates and configures FastAPI."""
    app = FastAPI(
        title="Véktor API",
        version="1.0.0",
        description="Plataforma SaaS de salud financiera para negocios argentinos.",
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

    # ── 500 legible por el navegador ─────────────────────────────────────────
    # Va justo ADENTRO de CORS (el último `add_middleware` queda más afuera):
    # así el 500 sube por CORS y por el logger que pone `X-Trace-Id`. Ver
    # `unhandled_errors.py`.
    app.add_middleware(UnhandledErrorMiddleware, handler=_respuesta_500)

    # ── CORS ─────────────────────────────────────────────────────────────────
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.CORS_ORIGINS,
        allow_origin_regex=settings.CORS_ORIGIN_REGEX,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=CORS_ALLOW_HEADERS,
        expose_headers=CORS_EXPOSE_HEADERS,
    )

    # ── Security headers ──────────────────────────────────────────────────────
    @app.middleware("http")
    async def security_headers(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'none'"
        if settings.is_production:
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response

    # ── Request logging ───────────────────────────────────────────────────────
    @app.middleware("http")
    async def request_logger(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        import structlog.contextvars  # noqa: PLC0415

        from app.observability.trace import set_trace_id  # noqa: PLC0415

        request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
        # trace_id: una sola id de correlación por request (la del cliente si la manda).
        trace_id = request.headers.get("X-Trace-Id") or request_id

        # Reset per-request context and pre-bind known fields
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(
            environment=settings.ENVIRONMENT,
            request_id=request_id,
            trace_id=trace_id,
            method=request.method,
            endpoint=request.url.path,
        )
        # Disponible para el código de app (audit/external_operation logs) en este request.
        set_trace_id(trace_id)
        # Correlación con Sentry: mismo id que structlog y que el header que
        # recibe el frontend.
        sentry_sdk.set_tag("vektor_trace_id", trace_id)

        t0 = time.monotonic()
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Trace-Id"] = trace_id
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
    @app.exception_handler(RequestValidationError)
    async def request_validation_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # Al log va QUÉ campo falló y por qué, nunca el valor: un 422 del alta o
        # del cambio de clave llevaba la contraseña y el PIN en texto plano a los
        # logs de Railway (cuerpo crudo + `input` de Pydantic; revisión profunda
        # 2026-10-24, E0-B-4). El cliente sigue recibiendo el detalle completo.
        logger.warning(
            "request.body_validation_error",
            path=str(request.url.path),
            errors=errores_para_log(exc.errors()),
        )
        # `jsonable_encoder` NO es decorativo (y es lo que hace el handler default
        # de FastAPI): cuando la validación viene de un `field_validator` o un
        # `model_validator` que levanta `ValueError`, Pydantic v2 mete la excepción
        # cruda en `error["ctx"]["error"]`. Serializar eso directo tira
        # `TypeError: Object of type ValueError is not JSON serializable` y el 422
        # se convierte en un 500 — el cliente recibe "error del servidor" cuando en
        # realidad mandó un payload inválido.
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={"detail": jsonable_encoder(exc.errors())},
        )

    @app.exception_handler(ValidationError)
    async def pydantic_validation_handler(request: Request, exc: ValidationError) -> JSONResponse:
        logger.warning(
            "request.validation_error",
            path=str(request.url.path),
            errors=errores_para_log(exc.errors()),
        )
        # Esta validación es INTERNA (filas de la base, salida de un LLM): su
        # `input` son datos del servidor, no lo que mandó el cliente. Tampoco se
        # devuelven.
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={"detail": jsonable_encoder(errores_para_log(exc.errors()))},
        )

    async def subscription_error_handler(request: Request, exc: Exception) -> JSONResponse:
        # Red para los rechazos de suscripción/cupo que salen como excepción de
        # dominio y no como HTTPException — típicamente el consumo que va junto
        # con los datos (`consume_with_data`): ahí no se puede traducir en el
        # lugar, porque la excepción tiene que llegar cruda al `except` que
        # revierte el import y compensa el lease. Misma traducción que el resto.
        traducido = subscription_http_error(exc)
        logger.info(
            "subscription.rejected",
            path=str(request.url.path),
            error_type=type(exc).__name__,
            status_code=traducido.status_code,
        )
        return JSONResponse(status_code=traducido.status_code, content={"detail": traducido.detail})

    for _tipo in SUBSCRIPTION_ERRORS:
        app.add_exception_handler(_tipo, subscription_error_handler)

    @app.exception_handler(InsufficientStockError)
    async def insufficient_stock_handler(
        request: Request, exc: InsufficientStockError
    ) -> JSONResponse:
        # Venta EN VIVO sin stock suficiente: NO se permite stock negativo. 400 con el
        # mensaje canónico. La transacción ya revirtió (get_db_session hace rollback ante
        # la excepción), así que no queda SaleEntry ni movimiento persistido.
        logger.info(
            "sale.insufficient_stock",
            path=str(request.url.path),
            product_id=str(exc.product_id),
            available=exc.available,
            requested=exc.requested,
        )
        # `detail` sigue siendo el texto de siempre (lo leen clientes que ya existen);
        # `code` es el agregado. El flush de la cola offline necesita distinguir ESTE
        # 400 de cualquier otro para decidir si reintenta capturando la discrepancia,
        # y hacerlo por el texto del mensaje se rompería con cualquier cambio de copy.
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={
                "detail": exc.user_message,
                "code": exc.CODE,
                "product_id": str(exc.product_id),
                "available": exc.available,
                "requested": exc.requested,
            },
        )

    @app.exception_handler(VentaSinCompraError)
    async def sale_without_purchase_handler(
        request: Request, exc: VentaSinCompraError
    ) -> JSONResponse:
        # B·5e: toda venta con compra previa. 422 con el producto: la pantalla
        # ofrece cargar la compra o el stock inicial. Rollback como cualquier error.
        logger.info(
            "sale.without_purchase", path=str(request.url.path), product_id=str(exc.product_id)
        )
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={
                "detail": {
                    "code": "SALE_WITHOUT_PURCHASE",
                    "message": exc.user_message,
                    "product_id": str(exc.product_id),
                    "product_name": exc.nombre,
                }
            },
        )

    @app.exception_handler(VentaSinProductoError)
    async def sale_needs_product_handler(
        request: Request, exc: VentaSinProductoError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={"detail": {"code": "SALE_NEEDS_PRODUCT", "message": exc.user_message}},
        )

    @app.exception_handler(SaleProductNotFoundError)
    async def sale_product_not_found_handler(
        request: Request, exc: SaleProductNotFoundError
    ) -> JSONResponse:
        # product_id de una venta inexistente / de otro tenant → 400 (no 500), mismo
        # criterio que manual-batch. La transacción revierte (get_db_session).
        logger.info(
            "sale.product_not_found",
            path=str(request.url.path),
            product_id=str(exc.product_id),
        )
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"detail": exc.user_message},
        )

    # Última red: sólo llega acá lo que falla AFUERA de CORS (en los propios
    # middlewares externos). Lo de adentro lo atrapa `UnhandledErrorMiddleware`.
    app.add_exception_handler(Exception, _respuesta_500)

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
