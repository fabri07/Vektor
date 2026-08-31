"""Bloque 5 — persistencia de decisiones EXPLÍCITAS de mapeo por huella de
esquema (tenant + schema_fingerprint + context_signature + decision_type).

Nunca guarda sugerencias automáticas: cada `record_*` solo escribe lo que el
caller le pasa explícitamente, y el caller (``_insert_multisheet_data``) solo
pasa lo que vino en `context_mappings`/`context_entity`/`context_confirmed`/
`stock_treatment`/`shipping_decisions` — dicts que, por convención ya
establecida en todo el pipeline de ingestión (ver `ColumnMapperPanel`/
`FileInterpretationReview` en el frontend), contienen SOLO lo que el usuario
tocó, nunca el default ni la sugerencia heurística. Una hoja derivada excluida
por default nunca aparece en `context_confirmed`, así que nunca se registra
acá — no hace falta un chequeo especial para eso.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.ingestion_schema_fingerprint import (
    compute_context_signature,
    compute_schema_fingerprint,
)
from app.persistence.models.ingestion_schema_decision import (
    CURRENT_DECISION_FORMAT_VERSION,
    IngestionSchemaDecision,
)


async def _upsert_decision(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    schema_fingerprint: str,
    context_signature: str,
    decision_type: str,
    payload: dict[str, Any],
) -> None:
    """Idempotente: misma clave → actualiza el payload existente en vez de
    duplicar (la restricción única de la migración es el cinturón, esto evita
    pagar un IntegrityError en el camino feliz)."""
    existing = (
        await session.execute(
            select(IngestionSchemaDecision).where(
                IngestionSchemaDecision.tenant_id == tenant_id,
                IngestionSchemaDecision.schema_fingerprint == schema_fingerprint,
                IngestionSchemaDecision.context_signature == context_signature,
                IngestionSchemaDecision.decision_type == decision_type,
            )
        )
    ).scalar_one_or_none()
    if existing is None:
        session.add(
            IngestionSchemaDecision(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                schema_fingerprint=schema_fingerprint,
                context_signature=context_signature,
                decision_type=decision_type,
                payload=payload,
                format_version=CURRENT_DECISION_FORMAT_VERSION,
            )
        )
        await session.flush()
        return
    existing.payload = payload
    existing.format_version = CURRENT_DECISION_FORMAT_VERSION


async def record_context_decisions(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    schema_fingerprint: str,
    context_signature: str,
    *,
    column_mapping: dict[str, str] | None = None,
    context_entity: str | None = None,
    context_included: bool | None = None,
    stock_treatment: str | None = None,
    shipping_decision: str | None = None,
) -> None:
    """Registra las decisiones EXPLÍCITAS de UN contexto. Cada parámetro en
    ``None`` significa "el usuario no tocó esto ahora" — no se escribe nada
    para ese `decision_type` (no se borra lo que ya había, tampoco se inventa
    un default)."""
    if column_mapping:
        await _upsert_decision(
            session, tenant_id, schema_fingerprint, context_signature,
            "column_mapping", {"mapping": column_mapping},
        )
    if context_entity is not None:
        await _upsert_decision(
            session, tenant_id, schema_fingerprint, context_signature,
            "context_entity", {"entity": context_entity},
        )
    if context_included is not None:
        await _upsert_decision(
            session, tenant_id, schema_fingerprint, context_signature,
            "context_included", {"included": context_included},
        )
    if stock_treatment is not None:
        await _upsert_decision(
            session, tenant_id, schema_fingerprint, context_signature,
            "stock_treatment", {"treatment": stock_treatment},
        )
    if shipping_decision is not None:
        await _upsert_decision(
            session, tenant_id, schema_fingerprint, context_signature,
            "shipping_decision", {"decision": shipping_decision},
        )


async def lookup_context_decisions(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    schema_fingerprint: str,
    context_signature: str,
) -> dict[str, Any]:
    """``{decision_type: payload}`` de lo confirmado para este esquema/contexto.

    Solo filas en el formato VIGENTE (`format_version` actual) — una fila
    escrita con un formato viejo queda invisible hasta que se vuelva a
    escribir (no se migra en el lugar, no hace falta script de backfill)."""
    rows = (
        await session.execute(
            select(IngestionSchemaDecision).where(
                IngestionSchemaDecision.tenant_id == tenant_id,
                IngestionSchemaDecision.schema_fingerprint == schema_fingerprint,
                IngestionSchemaDecision.context_signature == context_signature,
                IngestionSchemaDecision.format_version == CURRENT_DECISION_FORMAT_VERSION,
            )
        )
    ).scalars().all()
    return {row.decision_type: row.payload for row in rows}


async def lookup_remembered_decisions_for_contexts(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    file_type: str,
    contexts: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Bloque 5 (consumo): decisiones recordadas para PRELLENAR un preview
    nuevo — ``{context_id: {decision_type: payload}}``. Gateado adentro (no en
    cada caller) por el mismo flag de rollout que graba: con el tenant fuera
    del rollout, comportamiento idéntico al de hoy — no busca nada, el preview
    no cambia. La huella se calcula UNA vez para todo el archivo (mismo
    criterio que ``insert_confirmed_data``) y por-contexto para cada hoja; una
    hoja sin ``context_id`` no tiene con qué recordar y se saltea."""
    from app.config.ingestion_schema_decisions_rollout import (  # noqa: PLC0415
        ingestion_schema_decisions_enabled_for,
    )

    if not contexts or not ingestion_schema_decisions_enabled_for(tenant_id):
        return {}
    schema_fingerprint = compute_schema_fingerprint(file_type, contexts)
    result: dict[str, dict[str, Any]] = {}
    for ctx in contexts:
        context_id = ctx.get("context_id")
        if not context_id:
            continue
        context_signature = compute_context_signature(ctx)
        decisions = await lookup_context_decisions(
            session, tenant_id, schema_fingerprint, context_signature
        )
        if decisions:
            result[str(context_id)] = decisions
    return result
