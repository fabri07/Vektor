"""ColumnMappingService: sugerencias de mapeo de columnas + aprendizaje por tenant."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.observability.logger import get_logger

logger = get_logger(__name__)

# ── Campos canónicos por entity_type ─────────────────────────────────────────
CANONICAL_FIELDS: dict[str, dict[str, str]] = {
    "sale": {
        "amount": "Monto de venta",
        "transaction_date": "Fecha de venta",
        "quantity": "Cantidad",
        "payment_method": "Método de pago",
        "product_name": "Nombre del producto",
        "notes": "Notas",
    },
    "expense": {
        "amount": "Monto del gasto",
        "expense_date": "Fecha del gasto",
        "category": "Categoría",
        "supplier_name": "Proveedor",
        "notes": "Notas",
    },
    "product": {
        "sku": "Código (SKU)",
        "name": "Nombre",
        "sale_price_ars": "Precio de venta",
        "unit_cost_ars": "Costo unitario",
        "stock_units": "Stock (unidades)",
        "category": "Categoría",
        "description": "Descripción",
    },
}

# Campos mínimos requeridos por entity_type
REQUIRED_FIELDS: dict[str, list[str]] = {
    "sale": ["amount", "transaction_date"],
    "expense": ["amount", "expense_date"],
    "product": ["name"],
}

# ── Heurísticas: entity_type → target_field → keywords (substring match) ─────
_HEURISTICS: dict[str, dict[str, set[str]]] = {
    "sale": {
        "amount": {
            "precio_venta",
            "venta",
            "ventas",
            "ingreso",
            "monto",
            "importe",
            "total_venta",
            "total_cobrado",
            "cobro",
            "total",
            "valor",
        },
        "transaction_date": {"fecha", "date", "dia", "mes", "periodo"},
        "quantity": {"cantidad", "qty", "unidades", "cant", "items", "unidad"},
        "payment_method": {"metodo", "medio", "pago", "forma_pago", "payment"},
        "product_name": {
            "producto",
            "descripcion",
            "nombre",
            "articulo",
            "item",
            "name",
            "concepto",
            "detalle",
        },
        "notes": {"notas", "observaciones", "obs", "comentarios", "nota", "memo"},
    },
    "expense": {
        "amount": {
            "costo",
            "gasto",
            "gastos",
            "egreso",
            "compra",
            "pago",
            "monto",
            "importe",
            "total",
            "valor",
        },
        "expense_date": {"fecha", "date", "dia", "mes", "periodo"},
        "category": {"categoria", "tipo", "rubro", "clasificacion", "concepto"},
        "supplier_name": {
            "proveedor",
            "proveedor_nombre",
            "empresa",
            "nombre_proveedor",
            "supplier",
        },
        "notes": {"notas", "observaciones", "descripcion", "detalle", "obs"},
    },
    "product": {
        "sku": {"sku", "codigo", "código", "code", "ref", "id_producto"},
        "name": {
            "producto",
            "descripcion",
            "descripción",
            "nombre",
            "articulo",
            "artículo",
            "item",
            "name",
            "concepto",
            "detalle",
        },
        "sale_price_ars": {"precio_venta", "precio", "price", "p_venta", "venta"},
        "unit_cost_ars": {"costo", "cost", "precio_costo", "p_costo", "costo_unitario"},
        "stock_units": {
            "stock",
            "cantidad",
            "inventario",
            "units",
            "qty",
            "existencia",
            "unidades",
        },
        "category": {"categoria", "tipo", "rubro"},
        "description": {"descripcion", "descripción", "detalle", "comentarios"},
    },
}


def _normalize_col(col: str) -> str:
    """Normalizar header para matching: lowercase + underscore."""
    return col.lower().strip().replace(" ", "_").replace("-", "_")


def _heuristic_match(normalized: str, entity_type: str) -> str | None:
    """Busca el target_field por substring en las heurísticas."""
    heuristics = _HEURISTICS.get(entity_type, {})
    for target_field, keywords in heuristics.items():
        if any(k in normalized for k in keywords):
            return target_field
    return None


def _fuzzy_match(normalized: str, entity_type: str) -> tuple[str | None, float]:
    """Similitud fuzzy entre nombre normalizado y keywords. Retorna (target_field, ratio)."""
    try:
        from rapidfuzz import fuzz
    except ImportError:
        return None, 0.0

    heuristics = _HEURISTICS.get(entity_type, {})
    best_target: str | None = None
    best_ratio = 0.0
    for target_field, keywords in heuristics.items():
        for kw in keywords:
            ratio = fuzz.ratio(normalized, kw) / 100.0
            if ratio > best_ratio:
                best_ratio = ratio
                best_target = target_field
    if best_ratio >= 0.70:
        return best_target, best_ratio
    return None, 0.0


class ColumnMappingService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def suggest_mappings(
        self,
        tenant_id: uuid.UUID,
        entity_type: str,
        headers: list[str],
        sample_rows: list[dict[str, Any]],
        *,
        trace_id: uuid.UUID | str | None = None,
        file_id: uuid.UUID | str | None = None,
    ) -> list[dict[str, Any]]:
        """Genera sugerencias de mapeo para los headers del archivo.

        FASE 2 (A2): si `trace_id` y `file_id` están presentes, la decisión de la
        4ª capa LLM se traza en pipeline_events (stage="mapping"). Los parámetros
        son keyword-only y opcionales para no romper los callers existentes.
        """
        from app.persistence.models.column_mapping import TenantColumnMapping  # noqa: PLC0415

        # Cargar historial del tenant para este entity_type
        result = await self.db.execute(
            select(TenantColumnMapping).where(
                TenantColumnMapping.tenant_id == tenant_id,
                TenantColumnMapping.entity_type == entity_type,
            )
        )
        history: dict[str, TenantColumnMapping] = {
            row.source_column: row for row in result.scalars().all()
        }

        required = set(REQUIRED_FIELDS.get(entity_type, []))
        suggestions: list[dict[str, Any]] = []

        for header in headers:
            normalized = _normalize_col(header)

            # Extraer sample values (hasta 5 no-nulos)
            sample_vals: list[str] = []
            for row in sample_rows[:10]:
                v = row.get(header)
                if v is not None and str(v).strip() not in ("", "None", "nan"):
                    sample_vals.append(str(v)[:50])
                if len(sample_vals) >= 5:
                    break

            target_field: str | None = None
            confidence: float = 0.0
            source: str = "none"

            # 1. Historial del tenant (prioridad máxima)
            if normalized in history:
                rec = history[normalized]
                target_field = rec.target_field
                confidence = min(0.99, 0.5 + rec.confirmed_count / 20.0)
                source = "tenant_history"

            # 2. Heurística global
            elif (heuristic := _heuristic_match(normalized, entity_type)) is not None:
                target_field = heuristic
                confidence = 0.75
                source = "heuristic"

            # 3. Fuzzy matching
            else:
                fuzzy_target, fuzzy_ratio = _fuzzy_match(normalized, entity_type)
                if fuzzy_target is not None:
                    target_field = fuzzy_target
                    confidence = fuzzy_ratio * 0.65  # escalar a rango 0–65%
                    source = "fuzzy"

            # Calcular status
            if target_field is not None and target_field != "ignore":
                status = "mapped"
            else:
                status = "unmapped"

            suggestions.append(
                {
                    "source_column": header,
                    "normalized_column": normalized,
                    "sample_values": sample_vals,
                    "target_field": target_field,
                    "confidence": round(confidence, 3),
                    "source": source,
                    "status": status,
                }
            )

        # FASE 2: 4ª capa LLM (fallback). Solo para columnas con baja confianza
        # determinística. Una sola llamada batch; fail-silent (flag/key/errores).
        await self._apply_llm_fallback(
            entity_type,
            suggestions,
            tenant_id=tenant_id,
            trace_id=trace_id,
            file_id=file_id,
        )

        # Segunda pasada: detectar required_missing
        mapped_targets = {
            s["target_field"] for s in suggestions if s["status"] == "mapped"
        }
        missing_required = required - mapped_targets

        # Si hay campos requeridos sin cubrir, marcar la primera columna sin mapear
        # cuyo nombre normalizado se acerque a algún campo requerido
        if missing_required:
            for s in suggestions:
                if s["status"] == "unmapped":
                    norm = s["normalized_column"]
                    for req_field in list(missing_required):
                        # Check si algún keyword del required field está en el nombre
                        req_keywords = _HEURISTICS.get(entity_type, {}).get(req_field, set())
                        if any(k in norm for k in req_keywords):
                            s["status"] = "required_missing"
                            missing_required.discard(req_field)
                            break

        return suggestions

    async def _apply_llm_fallback(
        self,
        entity_type: str,
        suggestions: list[dict[str, Any]],
        *,
        tenant_id: uuid.UUID | None = None,
        trace_id: uuid.UUID | str | None = None,
        file_id: uuid.UUID | str | None = None,
    ) -> None:
        """FASE 2: mejora las sugerencias de baja confianza con el LLM (in-place).

        Si `trace_id`/`file_id` están presentes, emite un pipeline_event con la
        traza antes/después de cada columna evaluada (qué decidió lo determinístico,
        qué decidió el LLM, y si lo pisó).
        """
        from app.application.services.llm_column_mapper import (  # noqa: PLC0415
            LLM_MAPPING_THRESHOLD,
            suggest_with_llm,
        )

        low_conf = [s for s in suggestions if s["confidence"] < LLM_MAPPING_THRESHOLD]
        if not low_conf:
            return
        valid_fields = CANONICAL_FIELDS.get(entity_type, {})
        if not valid_fields:
            return

        # Snapshot "antes" (la decisión determinística) para auditar qué pisó el LLM.
        before = {
            s["source_column"]: {
                "target_field": s["target_field"],
                "confidence": s["confidence"],
                "source": s["source"],
            }
            for s in low_conf
        }

        llm_result = await suggest_with_llm(
            entity_type,
            [{"header": s["source_column"], "sample_values": s["sample_values"]} for s in low_conf],
            valid_fields,
        )
        if not llm_result:
            return

        decisions: list[dict[str, Any]] = []
        for s in low_conf:
            hit = llm_result.get(s["source_column"])
            prev = before[s["source_column"]]
            overwritten = False
            if hit:
                target = hit["target_field"]
                conf = hit["confidence"]
                # Solo pisar si el LLM aporta un mapeo usable y MÁS confiable.
                if target != "ignore" and conf > s["confidence"]:
                    s["target_field"] = target
                    s["confidence"] = round(conf, 3)
                    s["source"] = "llm"
                    s["status"] = "mapped"
                    overwritten = True
            decisions.append(
                {
                    "column": s["source_column"],
                    "deterministic_target": prev["target_field"],
                    "deterministic_confidence": prev["confidence"],
                    "source_before": prev["source"],
                    "llm_target": hit["target_field"] if hit else None,
                    "llm_confidence": hit["confidence"] if hit else None,
                    "source_after": s["source"],
                    "final_target": s["target_field"],
                    "final_confidence": s["confidence"],
                    "overwritten": overwritten,
                }
            )

        await self._emit_mapping_event(
            tenant_id=tenant_id,
            trace_id=trace_id,
            file_id=file_id,
            entity_type=entity_type,
            decisions=decisions,
        )

    async def _emit_mapping_event(
        self,
        *,
        tenant_id: uuid.UUID | None,
        trace_id: uuid.UUID | str | None,
        file_id: uuid.UUID | str | None,
        entity_type: str,
        decisions: list[dict[str, Any]],
    ) -> None:
        """Traza la decisión del LLM de mapeo en pipeline_events (fail-silent).

        No emite si falta `trace_id`/`file_id`/`tenant_id` (callers que no pasan
        contexto de traza, p.ej. tests unitarios del mapeo)."""
        if trace_id is None or file_id is None or tenant_id is None:
            return
        from app.application.services import pipeline_event_service  # noqa: PLC0415
        from app.persistence.models.pipeline_event import STAGE_MAPPING  # noqa: PLC0415

        overwritten_count = sum(1 for d in decisions if d["overwritten"])
        await pipeline_event_service.emit_event(
            self.db,
            trace_id=trace_id,
            tenant_id=tenant_id,
            stage=STAGE_MAPPING,
            file_id=file_id,
            detail={
                "type": "column_mapping",
                "entity_type": entity_type,
                "columns_evaluated": len(decisions),
                "columns_overwritten": overwritten_count,
                "decisions": decisions,
            },
        )

    async def save_mappings(
        self,
        tenant_id: uuid.UUID,
        entity_type: str,
        confirmed: list[dict[str, str]],
    ) -> None:
        """Upsert de mapeos confirmados en tenant_column_mappings.

        No aprende mapeos "ignore" — cada archivo puede tener columnas distintas
        que ignorar. No aprende custom_fields tampoco (demasiado específicos).
        """
        from app.persistence.models.column_mapping import TenantColumnMapping  # noqa: PLC0415

        now = datetime.now(tz=UTC)

        for mapping in confirmed:
            source_col = _normalize_col(mapping["source_column"])
            target = mapping["target_field"]

            # No aprendemos "ignore" ni custom_fields
            if target == "ignore" or target.startswith("custom_field:"):
                continue

            result = await self.db.execute(
                select(TenantColumnMapping).where(
                    TenantColumnMapping.tenant_id == tenant_id,
                    TenantColumnMapping.entity_type == entity_type,
                    TenantColumnMapping.source_column == source_col,
                )
            )
            existing = result.scalar_one_or_none()

            if existing:
                if existing.target_field != target:
                    # Usuario cambió el mapeo → reiniciar contador
                    existing.target_field = target
                    existing.confirmed_count = 1
                else:
                    existing.confirmed_count += 1
                existing.last_seen_at = now
            else:
                self.db.add(
                    TenantColumnMapping(
                        id=uuid.uuid4(),
                        tenant_id=tenant_id,
                        entity_type=entity_type,
                        source_column=source_col,
                        target_field=target,
                        confirmed_count=1,
                        last_seen_at=now,
                        created_at=now,
                    )
                )

        await self.db.flush()
        logger.info(
            "column_mapping.saved",
            tenant_id=str(tenant_id),
            entity_type=entity_type,
            count=len(confirmed),
        )

    async def get_learned_mappings(self, tenant_id: uuid.UUID) -> list[Any]:
        """Retorna todos los mapeos aprendidos del tenant, ordenados por entity_type + source."""
        from app.persistence.models.column_mapping import TenantColumnMapping  # noqa: PLC0415

        result = await self.db.execute(
            select(TenantColumnMapping)
            .where(TenantColumnMapping.tenant_id == tenant_id)
            .order_by(TenantColumnMapping.entity_type, TenantColumnMapping.source_column)
        )
        return list(result.scalars().all())

    async def delete_mapping(
        self, tenant_id: uuid.UUID, mapping_id: uuid.UUID
    ) -> bool:
        """Elimina un mapeo aprendido. Retorna True si existía."""
        from app.persistence.models.column_mapping import TenantColumnMapping  # noqa: PLC0415

        result = await self.db.execute(
            select(TenantColumnMapping).where(
                TenantColumnMapping.id == mapping_id,
                TenantColumnMapping.tenant_id == tenant_id,
            )
        )
        existing = result.scalar_one_or_none()
        if not existing:
            return False
        await self.db.delete(existing)
        await self.db.flush()
        return True
