"""Read-only: ¿por qué «Otros» rechaza una venta histórica con «sin compra previa»?

CONTEXTO (pruebas del dueño, 2026-11-16)
----------------------------------------
Al vincular desde «Otros» un fiado de agosto u octubre a un producto, el gate B·5e
(``respaldo_service.exigir_respaldo``) lo rechaza si el PRIMER día con una fuente
con costo del producto (compra o stock inicial viva, qty > 0, costo > 0) es
posterior al día de la venta. Hay tres causas posibles y este script las separa:

1. ``apertura_posterior``: el producto SÍ tiene stock inicial, pero con la fecha de
   carga (placeholder sin confirmar) o una fecha elegida posterior a la venta.
   Se arregla confirmando desde cuándo se tenía el stock.
2. ``sin_fuente``: el producto no tiene ninguna fuente con costo (o se eligió un
   duplicado sin respaldo mientras la compra está en otro producto).
3. ``respaldada``: tiene fuente anterior; el rechazo viene de otro lado (stock
   actual insuficiente, por ejemplo).

SOLO SELECT. Nunca imprime la URL (la ``DATABASE_URL`` la provee el usuario).

Uso (desde ``backend/``):
    DATABASE_URL='postgresql://...' .venv/bin/python scripts/diag_apertura_producto.py <tenant_uuid>
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from datetime import UTC, date, datetime

from sqlalchemy import text

_SCRIPTS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _SCRIPTS)
sys.path.insert(0, os.path.dirname(_SCRIPTS))

from _db import async_engine_config  # noqa: E402

from app.persistence.db.engine_factory import crear_engine  # noqa: E402

# Una fila por producto activo con movimientos: su primera fuente con costo (misma
# regla del gate: fecha de negocio = COALESCE(occurred_at, created_at)) y su
# apertura de catálogo, con el estado de la confirmación de la fecha.
_SQL_PRODUCTOS = text(
    """
    SELECT p.id, p.name, p.stock_units, p.is_active,
           MIN(COALESCE(m.occurred_at, m.created_at)) FILTER (
               WHERE m.voided_at IS NULL AND m.qty > 0
                 AND COALESCE(m.unit_cost_final, m.unit_cost) > 0
                 AND (m.movement_type = 'purchase'
                      OR (m.movement_type = 'adjustment'
                          AND m.source_type = 'catalog_initial_stock'))
           ) AS primera_fuente,
           MIN(COALESCE(m.occurred_at, m.created_at)) FILTER (
               WHERE m.voided_at IS NULL AND m.source_type = 'catalog_initial_stock'
           ) AS apertura_en,
           BOOL_OR(m.opening_confirmed_at IS NOT NULL) FILTER (
               WHERE m.voided_at IS NULL AND m.source_type = 'catalog_initial_stock'
           ) AS apertura_confirmada,
           MAX(m.opening_date_source) FILTER (
               WHERE m.voided_at IS NULL AND m.source_type = 'catalog_initial_stock'
           ) AS fuente_de_la_fecha,
           BOOL_OR(m.source_upload_id IS NOT NULL) FILTER (
               WHERE m.voided_at IS NULL AND m.source_type = 'catalog_initial_stock'
           ) AS apertura_de_archivo
    FROM products p
    LEFT JOIN inventory_movements m
           ON m.product_id = p.id AND m.tenant_id = p.tenant_id
    WHERE p.tenant_id = :t
    GROUP BY p.id, p.name, p.stock_units, p.is_active
    ORDER BY p.name
    """
)

# Los registros pendientes de «Otros» (con su destino sugerido): sin producto
# resuelto no hay a qué compararlos, así que se listan con su fecha y lo que dice el
# archivo para cruzarlos a mano con la tabla de arriba.
_SQL_PENDIENTES = text(
    """
    SELECT r.id, r.row_data, r.suggested_entity
    FROM unclassified_records r
    WHERE r.tenant_id = :t AND r.status = 'PENDING'
    ORDER BY r.created_at
    """
)


def _dia(valor: object) -> date | None:
    if valor is None:
        return None
    dt = valor if isinstance(valor, datetime) else datetime.fromisoformat(str(valor))
    if dt.tzinfo is not None:
        dt = dt.astimezone(UTC)
    return dt.date()


def _p(titulo: str) -> None:
    print(f"\n{'=' * 70}\n  {titulo}\n{'=' * 70}")


async def main(tenant_id: uuid.UUID) -> None:
    url, connect_args = async_engine_config()
    engine = crear_engine(url, connect_args=connect_args)
    try:
        async with engine.connect() as conn:
            _p("PRODUCTOS: primera fuente con costo y estado de la apertura")
            filas = (await conn.execute(_SQL_PRODUCTOS, {"t": tenant_id})).all()
            for f in filas:
                primera = _dia(f.primera_fuente)
                apertura = _dia(f.apertura_en)
                estado = (
                    "sin_fuente"
                    if primera is None
                    else "apertura_sin_confirmar"
                    if f.apertura_en is not None and not f.apertura_confirmada
                    else "con_fuente"
                )
                print(
                    f"  {str(f.id)[:8]} | {f.name[:38]:38} | activo={f.is_active} "
                    f"stock={f.stock_units} | primera_fuente={primera} | "
                    f"apertura={apertura} confirmada={f.apertura_confirmada} "
                    f"fecha_via={f.fuente_de_la_fecha} de_archivo={f.apertura_de_archivo} "
                    f"-> {estado}"
                )

            _p("«OTROS»: registros pendientes (para cruzar con la tabla de arriba)")
            pend = (await conn.execute(_SQL_PENDIENTES, {"t": tenant_id})).all()
            if not pend:
                print("  (ninguna)")
            for r in pend:
                d = r.row_data or {}
                print(
                    f"  {str(r.id)[:8]} | sugerido={r.suggested_entity} | "
                    f"fecha={d.get('fecha') or d.get('date')} | "
                    f"producto={d.get('producto')} | detalle={d.get('detalle')} | "
                    f"monto={d.get('monto') or d.get('amount')}"
                )
    finally:
        await engine.dispose()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Uso: diag_apertura_producto.py <tenant_uuid>")
        sys.exit(2)
    asyncio.run(main(uuid.UUID(sys.argv[1])))
