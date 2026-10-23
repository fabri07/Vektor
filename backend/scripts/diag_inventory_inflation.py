"""Diagnóstico READ-ONLY de inflación de inventory_movements (cantidades duplicadas).

Cuantifica las tres causas candidatas de "cantidad comprada" inflada, por tenant:
  1. NO_EXPENSE     — movimientos purchase sin gasto COGS que los respalde
                      (reread huérfano + stock de catálogo sin gasto).
  2. DUP_PRODUCTS   — productos con el mismo nombre repetido (doble bucket single-sheet).
  3. REREAD_DUP     — clusters (product_id, día, qty) con >1 movimiento purchase idéntico
                      (firma de relectura no idempotente).
Y la divergencia SUM(movimientos purchase) vs stock_units por producto.

SOLO SELECT — nunca escribe, nunca imprime la connection URL. La DATABASE_URL la provee
el usuario desde su shell. Correr desde backend/.

Uso:
    DATABASE_URL='postgresql://...' .venv/bin/python \\
        scripts/diag_inventory_inflation.py --all-active
    ... scripts/diag_inventory_inflation.py --tenant <uuid> --detail
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import uuid
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _db import async_engine_config  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine  # noqa: E402


async def _tenant_rollup(session: AsyncSession, tid: uuid.UUID) -> dict[str, Any]:
    """Métricas de inflación de un tenant. Solo SELECT."""
    totals = (
        await session.execute(
            text(
                "SELECT COUNT(*) AS n_movs, COALESCE(SUM(qty), 0) AS sum_qty "
                "FROM inventory_movements "
                "WHERE tenant_id = :tid AND movement_type = 'purchase'"
            ),
            {"tid": tid},
        )
    ).mappings().one()

    # 1. NO_EXPENSE: movimientos purchase sin gasto del mismo product_id/fecha.
    no_exp = (
        await session.execute(
            text(
                "SELECT COUNT(*) AS n, COALESCE(SUM(im.qty), 0) AS q "
                "FROM inventory_movements im "
                "WHERE im.tenant_id = :tid AND im.movement_type = 'purchase' "
                "AND NOT EXISTS ("
                "  SELECT 1 FROM expense_entries e "
                "  WHERE e.tenant_id = im.tenant_id AND e.product_id = im.product_id "
                "  AND e.voided_at IS NULL "
                "  AND date(e.transaction_date) = date(im.created_at))"
            ),
            {"tid": tid},
        )
    ).mappings().one()

    # 2. DUP_PRODUCTS: nombres de producto repetidos (case-insensitive), activos.
    dup_prod = (
        await session.execute(
            text(
                "SELECT COUNT(*) AS grupos, COALESCE(SUM(c - 1), 0) AS sobrantes FROM ("
                "  SELECT lower(name) AS n, COUNT(*) AS c FROM products "
                "  WHERE tenant_id = :tid AND deactivated_at IS NULL "
                "  GROUP BY lower(name) HAVING COUNT(*) > 1) t"
            ),
            {"tid": tid},
        )
    ).mappings().one()

    # 3. REREAD_DUP: clusters (product_id, día, qty) con >1 movimiento purchase idéntico.
    reread = (
        await session.execute(
            text(
                "SELECT COUNT(*) AS clusters, COALESCE(SUM(c - 1), 0) AS movs_extra FROM ("
                "  SELECT product_id, date(created_at) AS d, qty, COUNT(*) AS c "
                "  FROM inventory_movements "
                "  WHERE tenant_id = :tid AND movement_type = 'purchase' "
                "  GROUP BY product_id, date(created_at), qty HAVING COUNT(*) > 1) t"
            ),
            {"tid": tid},
        )
    ).mappings().one()

    # 4. Divergencia agregada: SUM(movimientos purchase) vs SUM(stock_units).
    diverg = (
        await session.execute(
            text(
                "SELECT COALESCE(SUM(mov_qty), 0) AS sum_movs, "
                "       COALESCE(SUM(stock_units), 0) AS sum_stock FROM ("
                "  SELECT p.id, p.stock_units, "
                "    (SELECT COALESCE(SUM(qty), 0) FROM inventory_movements im "
                "     WHERE im.tenant_id = p.tenant_id AND im.product_id = p.id "
                "     AND im.movement_type = 'purchase') AS mov_qty "
                "  FROM products p WHERE p.tenant_id = :tid AND p.deactivated_at IS NULL) t"
            ),
            {"tid": tid},
        )
    ).mappings().one()

    return {
        "tenant_id": str(tid),
        "n_movs": int(totals["n_movs"]),
        "sum_qty": float(totals["sum_qty"]),
        "no_expense_movs": int(no_exp["n"]),
        "no_expense_qty": float(no_exp["q"]),
        "dup_product_groups": int(dup_prod["grupos"]),
        "dup_product_extra": int(dup_prod["sobrantes"]),
        "reread_dup_clusters": int(reread["clusters"]),
        "reread_dup_extra_movs": int(reread["movs_extra"]),
        "sum_purchase_movs_qty": float(diverg["sum_movs"]),
        "sum_stock_units": float(diverg["sum_stock"]),
    }


async def _detail_top_divergent(session: AsyncSession, tid: uuid.UUID, limit: int = 25) -> None:
    """Top productos por divergencia SUM(movimientos purchase) - stock_units. Solo SELECT."""
    rows = (
        await session.execute(
            text(
                "SELECT name, stock_units, n_movs, sum_qty FROM ("
                "  SELECT p.name, p.stock_units, "
                "    (SELECT COUNT(*) FROM inventory_movements im "
                "     WHERE im.tenant_id = p.tenant_id AND im.product_id = p.id "
                "     AND im.movement_type = 'purchase') AS n_movs, "
                "    (SELECT COALESCE(SUM(qty), 0) FROM inventory_movements im "
                "     WHERE im.tenant_id = p.tenant_id AND im.product_id = p.id "
                "     AND im.movement_type = 'purchase') AS sum_qty "
                "  FROM products p WHERE p.tenant_id = :tid AND p.deactivated_at IS NULL) t "
                "ORDER BY sum_qty - stock_units DESC LIMIT :lim"
            ),
            {"tid": tid, "lim": limit},
        )
    ).mappings().all()
    print(f"\n  Top {limit} productos por divergencia (SUM movimientos purchase − stock_units):")
    print(f"  {'producto':<40} {'stock':>8} {'#movs':>6} {'sum_qty':>9} {'diverg':>9}")
    for r in rows:
        diverg = float(r["sum_qty"]) - float(r["stock_units"])
        print(
            f"  {str(r['name'])[:40]:<40} {float(r['stock_units']):>8.0f} "
            f"{int(r['n_movs']):>6} {float(r['sum_qty']):>9.0f} {diverg:>9.0f}"
        )


def _print_rollup(r: dict[str, Any]) -> None:
    print(
        f"tenant {r['tenant_id']}: "
        f"{r['n_movs']} movs purchase (sum_qty={r['sum_qty']:.0f})"
    )
    print(
        f"    NO_EXPENSE: {r['no_expense_movs']} movs / qty {r['no_expense_qty']:.0f}  |  "
        f"DUP_PRODUCTS: {r['dup_product_groups']} grupos / {r['dup_product_extra']} sobrantes  |  "
        f"REREAD_DUP: {r['reread_dup_clusters']} clusters / {r['reread_dup_extra_movs']} movs extra"
    )
    print(
        f"    divergencia: SUM(movs purchase)={r['sum_purchase_movs_qty']:.0f} vs "
        f"SUM(stock_units)={r['sum_stock_units']:.0f} "
        f"(delta={r['sum_purchase_movs_qty'] - r['sum_stock_units']:.0f})"
    )


async def main() -> None:
    parser = argparse.ArgumentParser(description="Diagnóstico read-only de inflación de inventario")
    parser.add_argument("--tenant", help="UUID de tenant puntual")
    parser.add_argument("--all-active", action="store_true", help="Todos los tenants activos")
    parser.add_argument(
        "--detail", action="store_true", help="Top productos divergentes (solo con --tenant)"
    )
    args = parser.parse_args()

    if not args.tenant and not args.all_active:
        print("ERROR: indicá --tenant <uuid> o --all-active.")
        sys.exit(2)

    url, connect_args = async_engine_config()
    engine = create_async_engine(url, connect_args=connect_args)
    async with AsyncSession(engine) as session:
        if args.tenant:
            tids = [uuid.UUID(args.tenant)]
        else:
            rows = await session.execute(
                text("SELECT tenant_id FROM tenants WHERE status = 'ACTIVE'")
            )
            tids = [r[0] for r in rows.all()]

        print(f"[READ-ONLY] diagnóstico de inflación en {len(tids)} tenant(s)\n")
        for tid in tids:
            r = await _tenant_rollup(session, tid)
            if r["n_movs"] == 0:
                continue
            _print_rollup(r)
            if args.detail and args.tenant:
                await _detail_top_divergent(session, tid)
            print()
        await session.rollback()  # nunca escribimos; rollback defensivo
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
