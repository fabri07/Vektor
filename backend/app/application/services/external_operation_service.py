"""Registro unificado de operaciones externas (proveedores: Resend, WhatsApp, Meta, …).

`record_external_operation` inserta una fila en `external_operation_logs` con los
payloads redactados (sin secretos). El `trace_id` se autocaptura por el default de
la columna (contextvar del request).
"""

from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.models.external_operation_log import ExternalOperationLog

# Claves cuyo valor se redacta (case-insensitive, substring).
_REDACT_KEYS = ("token", "secret", "key", "authorization", "password", "api")
_REDACTED = "***"


def _redact(payload: dict[str, Any] | None) -> dict[str, Any] | None:
    """Redacta valores de claves sensibles (recursivo, shallow por nivel)."""
    if payload is None:
        return None
    out: dict[str, Any] = {}
    for k, v in payload.items():
        if any(s in k.lower() for s in _REDACT_KEYS):
            out[k] = _REDACTED
        elif isinstance(v, dict):
            out[k] = _redact(v)
        else:
            out[k] = v
    return out


async def record_external_operation(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    operation_type: str,
    provider: str,
    status: str,
    user_id: UUID | None = None,
    provider_account_id: str | None = None,
    provider_request_id: str | None = None,
    request_payload: dict[str, Any] | None = None,
    response_payload: dict[str, Any] | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
    latency_ms: int | None = None,
    attempt: int = 1,
) -> ExternalOperationLog:
    log = ExternalOperationLog(
        tenant_id=tenant_id,
        user_id=user_id,
        operation_type=operation_type,
        provider=provider,
        status=status,
        provider_account_id=provider_account_id,
        provider_request_id=provider_request_id,
        request_payload_redacted=_redact(request_payload),
        response_payload_redacted=_redact(response_payload),
        error_code=error_code,
        error_message=error_message,
        latency_ms=latency_ms,
        attempt=attempt,
    )
    session.add(log)
    await session.flush()
    return log
