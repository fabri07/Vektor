"""Read-only: de dónde sale el costo de cada producto de UN tenant, con sus movimientos.

Para decidir cómo corregir un costo cargado con el total del lote hay que ver sus
FUENTES: la procedencia que sella el producto (`custom_fields`: manual / catálogo /
origen) y los movimientos de compra y apertura (qty, unit_cost, unit_cost_final,
anulados). Imprime hasta 50 productos activos del tenant, los de mayor valor primero.

Uso (la DATABASE_URL la provee el usuario desde su shell; NUNCA se imprime):
    DATABASE_URL='postgresql://...' .venv/bin/python scripts/diag_costo_movimientos.py <tenant_uuid>

SOLO SELECT. Sin escrituras. Seguro contra producción.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _db import async_engine_config  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

_SQL_PRODUCTOS = text(
    "SELECT id, name, stock_units, unit_cost_ars, sale_price_ars, custom_fields "
    "FROM products WHERE tenant_id = :t AND is_active "
    "ORDER BY stock_units * COALESCE(unit_cost_ars, 0) DESC LIMIT 50"
)
_SQL_MOVIMIENTOS = text(
    "SELECT movement_type, qty, unit_cost, unit_cost_final, source_type, occurred_at, "
    "voided_at, document_ref, reason "
    "FROM inventory_movements WHERE tenant_id = :t AND product_id = :p "
    "ORDER BY COALESCE(occurred_at, created_at)"
)


async def main(tenant_id: uuid.UUID) -> None:
    url, connect_args = async_engine_config()
    engine = create_async_engine(url, connect_args=connect_args)
    try:
        async with engine.connect() as conn:
            productos = (await conn.execute(_SQL_PRODUCTOS, {"t": tenant_id})).all()
            for p in productos:
                cf = p.custom_fields or {}
                costo_cf = {
                    k: v for k, v in cf.items() if "costo" in k.lower() or "cost" in k.lower()
                }
                print(
                    f"\n== {p.id} | {p.name} | stock={p.stock_units} "
                    f"costo={p.unit_cost_ars} venta={p.sale_price_ars}"
                )
                texto_cf = json.dumps(costo_cf, ensure_ascii=False, default=str)
                print(f"   custom_fields(costo)={texto_cf}")
                movs = (
                    await conn.execute(_SQL_MOVIMIENTOS, {"t": tenant_id, "p": p.id})
                ).all()
                for m in movs:
                    anulado = " ANULADO" if m.voided_at else ""
                    print(
                        f"   - {m.movement_type:14} qty={m.qty} unit_cost={m.unit_cost} "
                        f"final={m.unit_cost_final} src={m.source_type} "
                        f"fecha={m.occurred_at} doc={m.document_ref}{anulado}"
                    )
    finally:
        await engine.dispose()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(2)
    asyncio.run(main(uuid.UUID(sys.argv[1])))
