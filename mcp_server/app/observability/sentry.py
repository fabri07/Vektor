"""
Sentry error tracking para el MCP server — init único + scrubbing.

Este servicio se despliega por separado del backend (Dockerfile, requirements
y paquete `app` propios), así que NO puede importar
`backend/app/observability/sentry.py`. El criterio se replica acá a propósito,
con el scrubbing endurecido para lo que este servicio realmente maneja:
credenciales de Google OAuth.

DSN vacío (default) = SDK deshabilitado (no-op), mismo criterio fail-soft que
el backend — Sentry nunca debe poder tumbar el boot.

Reporta al MISMO proyecto Sentry que el backend (`python-fastapi`), separado
por el tag `service=mcp`.

Usage:
    from app.observability.sentry import init_sentry
    init_sentry()
"""

from __future__ import annotations

import os
import re
from typing import Any

import sentry_sdk
from sentry_sdk.integrations.fastapi import FastApiIntegration
from sentry_sdk.integrations.starlette import StarletteIntegration
from sentry_sdk.types import Event, Hint, SamplingContext

from app.config import get_settings

_REDACTED = "[Filtered]"

# Este servicio custodia tokens de Google OAuth: el scrubbing por NOMBRE de
# campo cubre el vocabulario de OAuth (`code`, `state`, `*_token`,
# `client_secret`) además del genérico de auth y del PII que viaja en los
# perfiles de Google (email).
_SENSITIVE_KEY_RE = re.compile(
    r"(authoriz|token|password|passwd|secret|cookie|credential|"
    r"client_id|client_secret|refresh|access_token|id_token|"
    r"\bcode\b|\bstate\b|api[_-]?key|"
    r"email|mail|phone|telefono|cuit|dni)",
    re.IGNORECASE,
)
# Red de seguridad por VALOR: un token de Google puede caer en una clave
# genérica (`value`, `payload`) y ahí el filtro por nombre no lo ve.
_GOOGLE_TOKEN_VALUE_RE = re.compile(r"\b(ya29\.|1//|GOCSPX-)[A-Za-z0-9_\-./+]+")
_JWT_VALUE_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")
_EMAIL_VALUE_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
_CUIT_VALUE_RE = re.compile(r"\b\d{2}-?\d{8}-?\d\b")


def _redact_value(key: str, value: Any) -> Any:
    if _SENSITIVE_KEY_RE.search(key):
        return _REDACTED
    if isinstance(value, str) and (
        _GOOGLE_TOKEN_VALUE_RE.search(value)
        or _JWT_VALUE_RE.search(value)
        or _EMAIL_VALUE_RE.search(value)
        or _CUIT_VALUE_RE.search(value)
    ):
        return _REDACTED
    return value


def _scrub_mapping(data: dict[str, Any]) -> dict[str, Any]:
    return {key: _redact_value(key, value) for key, value in data.items()}


def _scrub_event(event: Event, hint: Hint) -> Event | None:
    """before_send: capa propia, aparte de `send_default_pii=False`."""
    request = event.get("request")
    if isinstance(request, dict):
        headers = request.get("headers")
        if isinstance(headers, dict):
            request["headers"] = _scrub_mapping(headers)
        # El callback de OAuth recibe `?code=...&state=...` en la query: es el
        # único lugar donde un código de autorización viaja en la URL. Se
        # descarta entera, sin intentar parsearla.
        if isinstance(request.get("query_string"), str):
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
    # Railway expone el SHA del commit en cada deploy — mismo criterio que el
    # backend, no hace falta setearlo a mano por ambiente.
    return os.getenv("RAILWAY_GIT_COMMIT_SHA") or os.getenv("GIT_SHA") or "unknown"


_EXCLUDED_PATHS = ("/health",)


def _traces_sampler(sampling_context: SamplingContext) -> float:
    """Excluye health checks y preflight CORS del sampling de performance."""
    settings = get_settings()

    asgi_scope = sampling_context.get("asgi_scope")
    if isinstance(asgi_scope, dict):
        if asgi_scope.get("method") == "OPTIONS":
            return 0.0
        if asgi_scope.get("path") in _EXCLUDED_PATHS:
            return 0.0

    transaction_context = sampling_context.get("transaction_context") or {}
    name = str(transaction_context.get("name") or "")
    # Formato "METHOD /path": comparar el path EXACTO tras el primer espacio,
    # nunca una substring del nombre completo.
    _, _, path = name.partition(" ")
    if path in _EXCLUDED_PATHS:
        return 0.0

    return settings.SENTRY_TRACES_SAMPLE_RATE


def init_sentry() -> None:
    settings = get_settings()

    if not settings.SENTRY_DSN:
        return

    if sentry_sdk.is_initialized():
        return

    sentry_sdk.init(
        dsn=settings.SENTRY_DSN,
        environment=settings.ENVIRONMENT,
        release=_resolve_release(),
        sample_rate=1.0,  # errores: 100% (independiente del sampling de performance)
        traces_sampler=_traces_sampler,
        send_default_pii=False,
        before_send=_scrub_event,
        integrations=[FastApiIntegration(), StarletteIntegration()],
    )
    sentry_sdk.set_tag("service", "mcp")
