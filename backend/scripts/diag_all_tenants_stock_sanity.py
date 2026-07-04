"""Read-only sanity check de stock para TODOS los tenants activos: (1) ningún
`products.stock_units` negativo, (2) ninguna divergencia (vía
`check_tenant_inventory_integrity`, el mismo cálculo — inicial de catálogo +
compras − ventas — reconciliado a mano para el incidente de "don pedro" en
2026-07, ahora generalizado a todos los tenants).

Usage:
    DATABASE_URL='postgresql://...neon.tech/vektor?sslmode=require' \
        .venv/bin/python scripts/diag_all_tenants_stock_sanity.py

ONLY runs SELECT statements. No writes. Safe against production. NUNCA imprime la
connection URL (la DATABASE_URL la provee el usuario desde su shell).
"""

from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _db import async_engine_config  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine  # noqa: E402

from app.application.services.inventory_integrity_service import (  # noqa: E402
    check_tenant_inventory_integrity,
)


async def main() -> None:
    url, connect_args = async_engine_config()
    engine = create_async_engine(url, connect_args=connect_args)
    async with AsyncSession(engine) as session:
        tenants = (
            await session.execute(
                text("SELECT tenant_id, display_name FROM tenants WHERE status = 'ACTIVE'")
            )
        ).all()
        print(f"Revisando {len(tenants)} tenant(s) activo(s)...\n")

        # ── 1. Stock negativo (chequeo global, un solo query) ────────────────
        negatives = (
            await session.execute(
                text(
                    "SELECT t.display_name, p.id, p.name, p.stock_units "
                    "FROM products p JOIN tenants t ON t.tenant_id = p.tenant_id "
                    "WHERE t.status = 'ACTIVE' AND p.stock_units < 0 "
                    "ORDER BY p.stock_units ASC"
                )
            )
        ).all()
        if negatives:
            print(f"⚠ {len(negatives)} producto(s) con stock_units NEGATIVO:")
            for r in negatives:
                print(f"  [{r.display_name}] {r.name}: {r.stock_units}")
        else:
            print("OK: ningún producto con stock_units negativo.")
        print()

        # ── 2. Divergencias vs reconciliación (por tenant) ───────────────────
        total_divergences = 0
        tenants_with_divergences: list[str] = []
        total_checked = 0
        for tid, display_name in tenants:
            result = await check_tenant_inventory_integrity(session, tid)
            total_checked += result["checked"]
            if result["divergences"]:
                total_divergences += len(result["divergences"])
                tenants_with_divergences.append(str(display_name))
                print(f"⚠ [{display_name}] {len(result['divergences'])} divergencia(s):")
                for d in result["divergences"]:
                    print(
                        f"    {d['product_name']}: sistema={d['stock_units']} "
                        f"esperado={d['stock_esperado']} diff={d['diff']}"
                    )

        print(f"\nProductos con ancla evaluados: {total_checked}")
        if total_divergences:
            print(
                f"⚠ {total_divergences} divergencia(s) en {len(tenants_with_divergences)} "
                f"tenant(s): {tenants_with_divergences}"
            )
        else:
            print("OK: ninguna divergencia detectada (dentro del umbral) en ningún tenant.")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
