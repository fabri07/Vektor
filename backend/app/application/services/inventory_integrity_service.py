"""Chequeo de integridad de inventario: detecta divergencia entre `products.stock_units`
y una expectativa reconstruida desde compras/ventas reales del ledger, para productos
con un ancla confiable (movimiento `catalog_initial_stock` vivo).

Puramente de lectura — NUNCA escribe `stock_units` ni ninguna otra columna. Nace del
incidente real de 2026-07 (tenant "don pedro"): una reconciliación manual contra
archivos fuente reveló que `stock_units` estaba inflado por movimientos `adjustment`
sin procedencia rastreable, algo que hoy nadie detecta automáticamente para el resto
de los tenants. Este servicio generaliza esa reconciliación (inicial + compras −
ventas) para que corra sola, sin reconstrucción manual caso por caso.

Sigue el patrón de query de `backend/scripts/diag_product_stock_reconstruction.py`
(agrupa `inventory_movements` vivos por tipo + suma `sales_entries.quantity`).

La fórmula suma, además del ancla y las compras:
- `adjustment` con `source_type in (reconciliation, manual_adjustment)`: correcciones
  deliberadas y auditadas — el CHECK `ck_inventory_movements_adjustment_source_type`
  (migración `20260728_0001`) ya exige que todo `adjustment` tenga uno de estos dos
  `source_type`, así que son confiables.
- `loss` (merma): viene de `stock_service.register_stock_loss`, auditado, y su `qty`
  ya es negativa en el ledger.

Se sigue salteando (`skipped_complex_ledger`) cualquier producto con: `sale`/`return`
en el ledger (las ventas se cuentan desde `sales_entries`, no desde
`inventory_movements` — contarlas dos veces duplicaría), o un `adjustment` sin
`source_type` tagueado (dato legacy previo al CHECK, no auditable con confianza).

No-invention: nunca concluye ni corrige — solo reporta divergencias con sus números,
para que un humano decida. La persistencia de la alerta (Notification/
DecisionAuditLog) es responsabilidad del llamador (endpoint admin o job), no de esta
función.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.inventory_movement_origin import (
    SOURCE_CATALOG_INITIAL_STOCK,
    SOURCE_MANUAL_ADJUSTMENT,
    SOURCE_RECONCILIATION,
)

_TAGGED_ADJUSTMENT_SOURCES = frozenset({SOURCE_RECONCILIATION, SOURCE_MANUAL_ADJUSTMENT})

_UUID_PARAM = PG_UUID(as_uuid=True)

# Umbral de divergencia: ambas condiciones deben cumplirse para reportar (evita ruido
# en productos de bajo stock donde 1-2 unidades de diferencia son normales).
_DEFAULT_ABSOLUTE_FLOOR_UNITS = 5
_DEFAULT_RELATIVE_THRESHOLD_PCT = 0.10


async def check_tenant_inventory_integrity(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    relative_threshold_pct: float = _DEFAULT_RELATIVE_THRESHOLD_PCT,
    absolute_floor_units: int = _DEFAULT_ABSOLUTE_FLOOR_UNITS,
) -> dict[str, Any]:
    """Escanea los productos del tenant con ancla confiable y reporta divergencias.

    Devuelve ``{tenant_id, divergences: [...], skipped_no_anchor: n,
    skipped_complex_ledger: n, checked: n, threshold: {...}}``. No escribe nada.
    """
    candidates = (
        await session.execute(
            text(
                "SELECT DISTINCT p.id, p.name, p.stock_units "
                "FROM products p "
                "JOIN inventory_movements im "
                "  ON im.tenant_id = p.tenant_id AND im.product_id = p.id "
                "WHERE p.tenant_id = :tid "
                "AND im.source_type = :anchor_source AND im.voided_at IS NULL"
            ).bindparams(bindparam("tid", type_=_UUID_PARAM)),
            {"tid": tenant_id, "anchor_source": SOURCE_CATALOG_INITIAL_STOCK},
        )
    ).mappings().all()

    total_candidates = len(candidates)
    total_products = (
        await session.execute(
            text("SELECT COUNT(*) FROM products WHERE tenant_id = :tid").bindparams(
                bindparam("tid", type_=_UUID_PARAM)
            ),
            {"tid": tenant_id},
        )
    ).scalar_one()
    # Informativo: cuántos productos del tenant no tienen ancla confiable (nunca
    # pasaron por un import de catálogo con stock inicial) y por eso ni se evalúan.
    skipped_no_anchor = max(0, int(total_products) - total_candidates)
    skipped_complex_ledger = 0
    divergences: list[dict[str, Any]] = []

    for prod in candidates:
        # SQLite (tests) devuelve el UUID de una query text() como hex plano (sin
        # guiones), no como uuid.UUID — normalizar antes de volver a bindearlo.
        raw_pid = prod["id"]
        pid = raw_pid if isinstance(raw_pid, uuid.UUID) else uuid.UUID(str(raw_pid))
        by_type = (
            await session.execute(
                text(
                    "SELECT movement_type, source_type, COALESCE(SUM(qty), 0) AS total_qty "
                    "FROM inventory_movements "
                    "WHERE tenant_id = :tid AND product_id = :pid AND voided_at IS NULL "
                    "GROUP BY movement_type, source_type"
                ).bindparams(
                    bindparam("tid", type_=_UUID_PARAM), bindparam("pid", type_=_UUID_PARAM)
                ),
                {"tid": tenant_id, "pid": pid},
            )
        ).mappings().all()

        anchor_qty = 0
        purchase_qty = 0
        tagged_adjustment_qty = 0
        loss_qty = 0
        has_other_movement_types = False
        for row in by_type:
            movement_type = row["movement_type"]
            source_type = row["source_type"]
            if source_type == SOURCE_CATALOG_INITIAL_STOCK:
                anchor_qty += int(row["total_qty"])
            elif movement_type == "purchase":
                purchase_qty += int(row["total_qty"])
            elif movement_type == "adjustment" and source_type in _TAGGED_ADJUSTMENT_SOURCES:
                # Correcciones deliberadas y auditadas (reconciliación o ajuste manual
                # con procedencia) — blindadas por el CHECK de la migración 20260728_0001.
                tagged_adjustment_qty += int(row["total_qty"])
            elif movement_type == "loss":
                # La merma ya viene negativa en el ledger.
                loss_qty += int(row["total_qty"])
            else:
                # sale/return (duplicaría sales_entries) o adjustment sin source_type
                # tagueado (legacy, no auditable): la fórmula no los contempla —
                # saltear en vez de arriesgar un falso positivo.
                has_other_movement_types = True

        if has_other_movement_types:
            skipped_complex_ledger += 1
            continue

        sales = (
            await session.execute(
                text(
                    "SELECT COALESCE(SUM(quantity), 0) AS total_qty "
                    "FROM sales_entries "
                    "WHERE tenant_id = :tid AND product_id = :pid AND voided_at IS NULL"
                ).bindparams(
                    bindparam("tid", type_=_UUID_PARAM), bindparam("pid", type_=_UUID_PARAM)
                ),
                {"tid": tenant_id, "pid": pid},
            )
        ).scalar_one()

        stock_esperado = anchor_qty + purchase_qty + tagged_adjustment_qty + loss_qty - int(sales)
        stock_units = int(prod["stock_units"])
        diff = stock_units - stock_esperado
        relative_base = max(1, abs(stock_esperado))
        if abs(diff) > absolute_floor_units and abs(diff) / relative_base > relative_threshold_pct:
            divergences.append(
                {
                    "product_id": str(pid),
                    "product_name": prod["name"],
                    "stock_units": stock_units,
                    "stock_esperado": stock_esperado,
                    "diff": diff,
                    "anchor_qty": anchor_qty,
                    "purchase_qty": purchase_qty,
                    "tagged_adjustment_qty": tagged_adjustment_qty,
                    "loss_qty": loss_qty,
                    "sales_qty": int(sales),
                }
            )

    return {
        "tenant_id": str(tenant_id),
        "checked": total_candidates,
        "divergences": divergences,
        "skipped_no_anchor": skipped_no_anchor,
        "skipped_complex_ledger": skipped_complex_ledger,
        "threshold": {
            "relative_pct": relative_threshold_pct,
            "absolute_floor_units": absolute_floor_units,
        },
    }
