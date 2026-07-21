"""Pipeline event service — traza append-only del pipeline de ingestión. FASE 0.

`emit_event` es fail-silent: nunca rompe el pipeline. Escribe en la sesión del
caller (flush, sin commit propio) para que el evento entre en la misma
transacción que la operación que describe.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.observability.logger import get_logger
from app.persistence.models.pipeline_event import (
    STAGE_REJECT,
    PipelineEvent,
)

logger = get_logger(__name__)

# Máximo de filas de rechazo a persistir por evento (anti-bloat del JSONB).
_MAX_REJECTED_SAMPLE = 500


async def emit_event(
    session: AsyncSession,
    *,
    trace_id: uuid.UUID | str,
    tenant_id: uuid.UUID | str,
    stage: str,
    file_id: uuid.UUID | str | None = None,
    rows_in: int | None = None,
    rows_out: int | None = None,
    rows_rejected: int | None = None,
    latency_ms: int | None = None,
    confidence: str | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    """Inserta un PipelineEvent. Fail-silent: loguea y sigue ante cualquier error.

    El flush corre dentro de un SAVEPOINT propio (`begin_nested`): si el INSERT
    falla, se revierte SOLO ese savepoint (async-safe) y la transacción del caller
    queda limpia. Sin esto, un flush fallido dejaría el subtx de Postgres en
    estado abortado y envenenaría al caller (p.ej. abortaría un import ya exitoso
    cuando `emit_event` corre dentro del savepoint del confirm — F4).
    """
    try:
        event = PipelineEvent(
            trace_id=_as_uuid(trace_id),
            tenant_id=_as_uuid(tenant_id),
            file_id=_as_uuid(file_id) if file_id is not None else None,
            stage=stage,
            rows_in=rows_in,
            rows_out=rows_out,
            rows_rejected=rows_rejected,
            latency_ms=latency_ms,
            confidence=confidence,
            detail=detail,
        )
        async with session.begin_nested():
            session.add(event)
            await session.flush()
    except Exception as exc:  # noqa: BLE001 — la observabilidad nunca debe romper el pipeline
        logger.warning(
            "pipeline_event.emit_failed",
            stage=stage,
            trace_id=str(trace_id),
            error=str(exc),
        )


async def emit_rejections(
    session: AsyncSession,
    *,
    trace_id: uuid.UUID | str,
    tenant_id: uuid.UUID | str,
    file_id: uuid.UUID | str | None,
    rejected_rows: list[dict[str, Any]],
    reason: str,
) -> None:
    """Registra filas rechazadas (stage='reject') sin descartarlas en silencio.

    Persiste hasta `_MAX_REJECTED_SAMPLE` filas en `detail.rows`; el conteo total
    queda en `rows_rejected`. El crudo completo siempre está en R2.
    """
    if not rejected_rows:
        return
    sample = rejected_rows[:_MAX_REJECTED_SAMPLE]
    truncated = len(rejected_rows) > len(sample)
    await emit_event(
        session,
        trace_id=trace_id,
        tenant_id=tenant_id,
        stage=STAGE_REJECT,
        file_id=file_id,
        rows_rejected=len(rejected_rows),
        detail={
            "reason": reason,
            "rows": sample,
            "sample_truncated": truncated,
        },
    )


async def get_trace(
    session: AsyncSession,
    trace_id: uuid.UUID | str,
    tenant_id: uuid.UUID | str | None = None,
) -> list[dict[str, Any]]:
    """Devuelve todos los eventos de un upload, en orden cronológico."""
    stmt = select(PipelineEvent).where(PipelineEvent.trace_id == _as_uuid(trace_id))
    if tenant_id is not None:
        stmt = stmt.where(PipelineEvent.tenant_id == _as_uuid(tenant_id))
    stmt = stmt.order_by(PipelineEvent.created_at.asc())
    rows = (await session.execute(stmt)).scalars().all()
    return [
        {
            "stage": r.stage,
            "file_id": str(r.file_id) if r.file_id else None,
            "rows_in": r.rows_in,
            "rows_out": r.rows_out,
            "rows_rejected": r.rows_rejected,
            "latency_ms": r.latency_ms,
            "confidence": r.confidence,
            "detail": r.detail,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]


async def get_stats(session: AsyncSession, since: str) -> list[dict[str, Any]]:
    """Agregados por stage desde `since` (ISO date): throughput, rechazos, P95 latencia."""
    result = await session.execute(
        text(
            """
            SELECT
                stage,
                count(*)                                                AS events,
                coalesce(sum(rows_in), 0)                               AS rows_in,
                coalesce(sum(rows_out), 0)                              AS rows_out,
                coalesce(sum(rows_rejected), 0)                         AS rows_rejected,
                percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms)
                    FILTER (WHERE latency_ms IS NOT NULL)               AS p95_latency_ms
            FROM pipeline_events
            WHERE created_at >= :since
            GROUP BY stage
            ORDER BY stage
            """
        ),
        {"since": since},
    )
    out: list[dict[str, Any]] = []
    for row in result.mappings():
        rows_in = int(row["rows_in"] or 0)
        rows_rej = int(row["rows_rejected"] or 0)
        out.append(
            {
                "stage": row["stage"],
                "events": int(row["events"]),
                "rows_in": rows_in,
                "rows_out": int(row["rows_out"] or 0),
                "rows_rejected": rows_rej,
                "reject_rate": round(rows_rej / rows_in, 4) if rows_in else None,
                "p95_latency_ms": (
                    int(row["p95_latency_ms"]) if row["p95_latency_ms"] is not None else None
                ),
            }
        )
    return out


def _as_uuid(value: uuid.UUID | str) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
