"""Read-only follow-up: por qué el chequeo de integridad dio 0 productos evaluados,
y detalle de las filas 'adjustment' sin source_type que bloquean la migración
20260728_0001.

Usage:
    DATABASE_URL='postgresql://...neon.tech/vektor?sslmode=require' \
        .venv/bin/python scripts/diag_followup_anchor_and_missing_source.py

ONLY runs SELECT statements. No writes. Safe against production.
"""

from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _db import async_engine_config  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine  # noqa: E402


def p(title: str) -> None:
    print(f"\n{'=' * 70}\n  {title}\n{'=' * 70}")


async def main() -> None:
    url, connect_args = async_engine_config()
    engine = create_async_engine(url, connect_args=connect_args)
    async with AsyncSession(engine) as session:
        p("Estado de todos los tenants (activo o no)")
        rows = (
            await session.execute(
                text(
                    "SELECT tenant_id, display_name, status FROM tenants "
                    "ORDER BY status, display_name"
                )
            )
        ).all()
        for r in rows:
            print(f"  {r.display_name!r:30s} status={r.status}  tenant_id={r.tenant_id}")

        p("Movimientos 'catalog_initial_stock' vivos, TODOS los tenants (sin filtrar status)")
        anchors = (
            await session.execute(
                text(
                    "SELECT t.display_name, t.status, COUNT(*) n "
                    "FROM inventory_movements im "
                    "JOIN tenants t ON t.tenant_id = im.tenant_id "
                    "WHERE im.source_type = 'catalog_initial_stock' AND im.voided_at IS NULL "
                    "GROUP BY t.display_name, t.status ORDER BY n DESC"
                )
            )
        ).all()
        if anchors:
            for r in anchors:
                print(f"  [{r.display_name}] status={r.status}  n={r.n}")
        else:
            print("  NINGUNO — ni un solo movimiento catalog_initial_stock vivo en TODA la base.")

        p("Movimientos 'catalog_initial_stock' TOTALES (incluye voideados) — ¿se voidearon?")
        anchors_all = (
            await session.execute(
                text(
                    "SELECT t.display_name, COUNT(*) n, "
                    "COUNT(*) FILTER (WHERE im.voided_at IS NOT NULL) voided "
                    "FROM inventory_movements im "
                    "JOIN tenants t ON t.tenant_id = im.tenant_id "
                    "WHERE im.source_type = 'catalog_initial_stock' "
                    "GROUP BY t.display_name ORDER BY n DESC"
                )
            )
        ).all()
        if anchors_all:
            for r in anchors_all:
                print(f"  [{r.display_name}] total={r.n}  voideados={r.voided}")
        else:
            print("  NINGUNO — nunca existieron (ni vivos ni voideados).")

        p("Adjustment vivos SIN source_type (bloquean la migración 20260728_0001) — detalle")
        missing = (
            await session.execute(
                text(
                    "SELECT t.display_name, p.name AS product_name, im.qty, im.created_at, "
                    "im.id "
                    "FROM inventory_movements im "
                    "JOIN tenants t ON t.tenant_id = im.tenant_id "
                    "LEFT JOIN products p ON p.id = im.product_id "
                    "WHERE im.movement_type = 'adjustment' AND im.voided_at IS NULL "
                    "AND im.source_type IS NULL "
                    "ORDER BY t.display_name, im.created_at"
                )
            )
        ).all()
        if missing:
            print(f"  {len(missing)} fila(s):")
            by_tenant: dict[str, int] = {}
            for r in missing:
                by_tenant[r.display_name] = by_tenant.get(r.display_name, 0) + 1
            for name, n in by_tenant.items():
                print(f"    [{name}] {n} fila(s)")
            print("\n  Detalle (primeras 20):")
            for r in missing[:20]:
                print(
                    f"    [{r.display_name}] {r.product_name!r} qty={r.qty} "
                    f"{r.created_at} id={r.id}"
                )
        else:
            print("  Ninguna (raro, dado que la migración falló — revisar).")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
