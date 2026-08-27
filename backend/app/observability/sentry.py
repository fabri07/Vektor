"""
Sentry error tracking — init único + scrubbing.

Un solo punto de init reusado por los 3 procesos backend (web/worker/beat,
mismo Dockerfile/`start.sh`). DSN vacío (default) = SDK deshabilitado
(no-op), mismo criterio fail-soft que MCP_SERVER_URL="" en settings.py —
Sentry nunca debe poder tumbar el boot.

`send_default_pii=False` reduce lo que el SDK agrega automáticamente, pero
NO filtra lo que el código de Véktor agrega explícitamente (variables
locales del stacktrace, breadcrumbs, headers, extra). El `before_send`
(`_scrub_event`) es la capa que cubre eso — activa desde el día 1, no
diferida.

Usage:
    from app.observability.sentry import init_sentry
    init_sentry("web")
"""

import os
import re
from typing import Any, Literal

import sentry_sdk
from sentry_sdk.integrations.celery import CeleryIntegration
from sentry_sdk.integrations.fastapi import FastApiIntegration
from sentry_sdk.integrations.starlette import StarletteIntegration
from sentry_sdk.types import Event, Hint, SamplingContext

from app.config.settings import get_settings
from app.observability.logger import get_logger

logger = get_logger(__name__)

_REDACTED = "[Filtered]"

# Nombres de campo que nunca deben viajar a Sentry en texto plano. Cubre auth,
# identificadores fiscales/personales argentinos y datos de negocio (montos,
# nombres de clientes/proveedores) — no solo PII estricta.
_SENSITIVE_KEY_RE = re.compile(
    r"(authoriz|token|password|cookie|secret|"
    r"cuit|dni|email|phone|telefono|"
    r"amount|monto|customer_name|supplier_name|nombre_cliente|nombre_proveedor)",
    re.IGNORECASE,
)
# Red de seguridad adicional por VALOR, para el caso de una clave genérica
# (ej. `value`, `x`) que en los hechos contiene un CUIT/DNI.
_CUIT_VALUE_RE = re.compile(r"\b\d{2}-?\d{8}-?\d\b")
_DNI_VALUE_RE = re.compile(r"\b\d{7,8}\b")


def _redact_value(key: str, value: Any) -> Any:
    if _SENSITIVE_KEY_RE.search(key):
        return _REDACTED
    if isinstance(value, str) and (_CUIT_VALUE_RE.search(value) or _DNI_VALUE_RE.search(value)):
        return _REDACTED
    return value


def _scrub_mapping(data: dict[str, Any]) -> dict[str, Any]:
    return {key: _redact_value(key, value) for key, value in data.items()}


def _scrub_event(event: Event, hint: Hint) -> Event | None:
    """before_send: scrubbing propio, capa aparte de `send_default_pii=False`."""
    request = event.get("request")
    if isinstance(request, dict):
        headers = request.get("headers")
        if isinstance(headers, dict):
            request["headers"] = _scrub_mapping(headers)
        query_string = request.get("query_string")
        if isinstance(query_string, str) and _SENSITIVE_KEY_RE.search(query_string):
            request["query_string"] = _REDACTED

    exception = event.get("exception")
    if isinstance(exception, dict):
        for exc_value in exception.get("values") or []:
            stacktrace = exc_value.get("stacktrace") or {}
            for frame in stacktrace.get("frames") or []:
                frame_vars = frame.get("vars")
                if isinstance(frame_vars, dict):
                    frame["vars"] = _scrub_mapping(frame_vars)

    breadcrumbs = event.get("breadcrumbs")
    if isinstance(breadcrumbs, dict):
        for crumb in breadcrumbs.get("values") or []:
            data = crumb.get("data")
            if isinstance(data, dict):
                crumb["data"] = _scrub_mapping(data)

    extra = event.get("extra")
    if isinstance(extra, dict):
        event["extra"] = _scrub_mapping(extra)

    return event


def _resolve_release() -> str:
    # Railway expone el SHA del commit automáticamente en cada deploy — no
    # hace falta setearlo a mano por ambiente.
    return os.getenv("RAILWAY_GIT_COMMIT_SHA") or os.getenv("GIT_SHA") or "unknown"


_EXCLUDED_PATHS = ("/health", "/ready")


def _traces_sampler(sampling_context: SamplingContext) -> float:
    """
    Excluye health checks y preflight CORS del sampling de performance.

    Comparación EXACTA de path, nunca substring: `"/health" in name` también
    matcheaba `/api/v1/health-scores/...` y `/api/v1/settings/health-config`
    (rutas de negocio reales), apagándoles el sampling de performance por
    completo.
    """
    settings = get_settings()

    asgi_scope = sampling_context.get("asgi_scope")
    if isinstance(asgi_scope, dict):
        if asgi_scope.get("method") == "OPTIONS":
            return 0.0
        if asgi_scope.get("path") in _EXCLUDED_PATHS:
            return 0.0

    transaction_context = sampling_context.get("transaction_context") or {}
    name = str(transaction_context.get("name") or "")
    # Formato "METHOD /path" (ej. "GET /health") — comparar el path exacto
    # tras el primer espacio, no una substring del nombre completo.
    _, _, path = name.partition(" ")
    if path in _EXCLUDED_PATHS:
        return 0.0

    return settings.SENTRY_TRACES_SAMPLE_RATE


def init_sentry(service: Literal["web", "worker", "beat"]) -> None:
    settings = get_settings()

    if not settings.SENTRY_DSN:
        logger.info("sentry.disabled", service=service)
        if settings.is_production:
            logger.warning("sentry.disabled_in_production", service=service)
        return

    # Celery (pool prefork) forkea procesos que pueden reimportar este módulo;
    # `is_initialized()` evita reinicializar/pisar el client ya activo.
    if sentry_sdk.is_initialized():
        return

    integrations: list[Any] = [CeleryIntegration(monitor_beat_tasks=(service == "beat"))]
    if service == "web":
        # El proceso web también encola tasks vía `.delay()` — necesita
        # CeleryIntegration para propagar el trace distribuido hacia la task.
        integrations += [FastApiIntegration(), StarletteIntegration()]

    sentry_sdk.init(
        dsn=settings.SENTRY_DSN,
        environment=settings.ENVIRONMENT,
        release=_resolve_release(),
        sample_rate=1.0,  # errores: 100% (independiente del sampling de performance)
        traces_sampler=_traces_sampler,
        send_default_pii=False,
        before_send=_scrub_event,
        integrations=integrations,
    )
    sentry_sdk.set_tag("service", service)
