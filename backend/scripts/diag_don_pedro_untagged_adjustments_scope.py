"""Read-only: alcance completo de las filas 'adjustment' vivas sin source_type para
un tenant (por defecto "don pedro"). Suma por producto y compara contra la
reconciliación ya hecha a mano (catálogo + compras + ventas) para los productos
conocidos, para decidir si esta población es ruido (como esos 3 productos) o
carga real de stock inicial hecha por un código viejo sin tagging.

Usage:
    DATABASE_URL='postgresql://...neon.tech/vektor?sslmode=require' \
        .venv/bin/python scripts/diag_don_pedro_untagged_adjustments_scope.py \
        --tenant ee2625dc-96b7-464c-bda3-7f7018cc2a5b

ONLY runs SELECT statements. No writes. Safe against production.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _db import async_engine_config  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine  # noqa: E402

_DEFAULT_TENANT = "ee2625dc-96b7-464c-bda3-7f7018cc2a5b"


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tenant", default=_DEFAULT_TENANT)
    args = parser.parse_args()
    tid = uuid.UUID(args.tenant)

    url, connect_args = async_engine_config()
    engine = create_async_engine(url, connect_args=connect_args)
    async with AsyncSession(engine) as session:
        rows = (
            await session.execute(
                text(
                    "SELECT p.id, p.name, p.stock_units, "
                    "COUNT(im.id) AS n_untagged, "
                    "COALESCE(SUM(im.qty), 0) AS sum_untagged, "
                    "MIN(im.qty) AS min_qty, MAX(im.qty) AS max_qty "
                    "FROM products p "
                    "JOIN inventory_movements im "
                    "  ON im.tenant_id = p.tenant_id AND im.product_id = p.id "
                    "WHERE p.tenant_id = :tid AND im.movement_type = 'adjustment' "
                    "AND im.voided_at IS NULL AND im.source_type IS NULL "
                    "GROUP BY p.id, p.name, p.stock_units "
                    "ORDER BY sum_untagged ASC"
                ),
                {"tid": tid},
            )
        ).all()

        print(f"{len(rows)} producto(s) con adjustment vivo sin source_type:\n")
        total_sum = 0
        for r in rows:
            total_sum += int(r.sum_untagged)
            print(
                f"  {r.name!r:40s} stock_units={r.stock_units:>6} "
                f"n={r.n_untagged:>3} Σ_untagged={r.sum_untagged:>6} "
                f"[{r.min_qty}..{r.max_qty}]"
            )
        print(f"\nΣ total de todos los adjustment sin source_type: {total_sum}")

        # Comparación puntual con los 3 productos ya reconciliados a mano.
        known = {
            "Coca Cola 1.5L": {"inicial": 36, "compras": 217, "ventas": 249},
            "Agua Villavicencio 1.5L": {"inicial": 36, "compras": 258, "ventas": 265},
            "Gomitas Trulala x100g": {"inicial": 24, "compras": 228, "ventas": 236},
        }
        by_name = {r.name: r for r in rows}
        print("\nComparación con la reconciliación manual (catálogo+compras-ventas):")
        for name, k in known.items():
            r = by_name.get(name)
            esperado = k["inicial"] + k["compras"] - k["ventas"]
            if r is None:
                print(f"  {name}: SIN filas adjustment sin source_type (¿ya se voidearon?)")
                continue
            print(
                f"  {name}: stock_units={r.stock_units}  esperado(reconciliado)={esperado}  "
                f"Σ_untagged_para_este_producto={r.sum_untagged}  "
                f"(stock_units - Σ_untagged = {int(r.stock_units) - int(r.sum_untagged)})"
            )

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
