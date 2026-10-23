"""Read-only pre-deploy check: ¿hay algún movimiento 'adjustment' VIVO sin
source_type antes de agregar el CHECK constraint
ck_inventory_movements_adjustment_source_type (migración 20260728_0001)?

Si esto devuelve filas, el CHECK constraint va a fallar al aplicarse en el deploy —
hay que decidir (tenant por tenant) si backfillear source_type='reconciliation' para
esas filas históricas, o excluirlas, ANTES de mergear esa migración.

Usage:
    DATABASE_URL='postgresql://...neon.tech/vektor?sslmode=require' \
        .venv/bin/python scripts/diag_adjustment_missing_source_type.py

ONLY runs SELECT statements. No writes. Safe against production. NUNCA imprime la
connection URL (la DATABASE_URL la provee el usuario desde su shell).
"""

from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _db import async_engine_config  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine  # noqa: E402


async def main() -> None:
    url, connect_args = async_engine_config()
    engine = create_async_engine(url, connect_args=connect_args)
    async with AsyncSession(engine) as session:
        rows = (
            await session.execute(
                text(
                    "SELECT tenant_id, COUNT(*) AS n "
                    "FROM inventory_movements "
                    "WHERE movement_type = 'adjustment' AND voided_at IS NULL "
                    "AND source_type IS NULL "
                    "GROUP BY tenant_id "
                    "ORDER BY n DESC"
                )
            )
        ).all()
        if not rows:
            print(
                "OK: no hay movimientos 'adjustment' vivos sin source_type. "
                "El CHECK constraint de la migración 20260728_0001 puede aplicarse "
                "sin remediación previa."
            )
        else:
            total = sum(r.n for r in rows)
            print(
                f"⚠ {total} movimiento(s) 'adjustment' vivo(s) SIN source_type en "
                f"{len(rows)} tenant(s) — el CHECK constraint FALLARÁ si se aplica "
                "tal cual. Remediar (ej. backfill source_type='reconciliation') o "
                "excluir estos tenants antes de mergear la migración 20260728_0001.\n"
            )
            for r in rows:
                print(f"  tenant_id={r.tenant_id}  n={r.n}")
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
