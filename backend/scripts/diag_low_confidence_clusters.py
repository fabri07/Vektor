"""Detalle read-only de los clusters LOW_CONFIDENCE de repair_inventory_ledger.py.

El reporte de repair_inventory_ledger.py solo trae un agregado por tenant (conteos).
Este script reutiliza la MISMA lógica de detección/clasificación (importa las funciones
del script) para listar el detalle de cada cluster LOW: producto, día, cantidad, costo
unitario y cuántos movimientos hay — para revisión humana antes de decidir si alguno
amerita acción manual.

Usage:
    DATABASE_URL='postgresql://...neon.tech/vektor?sslmode=require' \
        .venv/bin/python scripts/diag_low_confidence_clusters.py --tenant <uuid> --out low.csv

ONLY runs SELECT statements. No writes. Safe against production.
"""

import argparse
import asyncio
import csv
import uuid

from _db import async_engine_config
from repair_inventory_ledger import _LOW, _plan_b1_dedup
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tenant", required=True, help="UUID de tenant")
    parser.add_argument("--out", default="low_confidence_clusters.csv")
    args = parser.parse_args()

    tid = uuid.UUID(args.tenant)
    url, connect_args = async_engine_config()
    engine = create_async_engine(url, connect_args=connect_args)
    async with AsyncSession(engine) as session:
        b1 = await _plan_b1_dedup(session, tid)
        low_clusters = [c for c in b1["clusters"] if c["confidence"] == _LOW]
        print(f"{len(low_clusters)} cluster(s) LOW_CONFIDENCE de {len(b1['clusters'])} total.\n")

        rows: list[dict[str, object]] = []
        for c in low_clusters:
            name_row = (
                await session.execute(
                    text("SELECT name FROM products WHERE id = CAST(:pid AS uuid)"),
                    {"pid": c["product_id"]},
                )
            ).first()
            product_name = name_row[0] if name_row else "(producto no encontrado)"
            row = {**c, "product_name": product_name}
            rows.append(row)
            print(
                f"  {c['day']}  {product_name[:40]:40s}  qty={c['qty']}  "
                f"n={c['n']}  movement_type={c['movement_type']}"
            )

        if rows:
            with open(args.out, "w", newline="", encoding="utf-8") as fh:
                writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
                writer.writeheader()
                writer.writerows(rows)
            print(f"\nReporte escrito en {args.out} ({len(rows)} fila(s)).")
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
