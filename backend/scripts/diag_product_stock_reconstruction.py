"""Reconstrucción read-only de stock por producto: agrupa TODOS los inventory_movements
vivos por movement_type (con suma de qty) y las ventas vivas (sales_entries.quantity),
para comparar contra products.stock_units actual. NO recomputa ni escribe nada — es
input para que un humano decida la corrección correcta (stock_units no es Σ(ledger),
puede tener alta no-ledger: manual, chat, catálogo absoluto).

Usage:
    DATABASE_URL='...' .venv/bin/python scripts/diag_product_stock_reconstruction.py \
        --tenant <uuid> --product "Coca Cola 1.5L" [--product "Otro" ...]

ONLY runs SELECT statements. No writes. Safe against production.
"""

import argparse
import asyncio
import uuid

from _db import async_engine_config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--product", required=True, action="append", dest="products")
    args = parser.parse_args()

    tid = uuid.UUID(args.tenant)
    url, connect_args = async_engine_config()
    engine = create_async_engine(url, connect_args=connect_args)
    async with AsyncSession(engine) as session:
        for name in args.products:
            prod = (
                await session.execute(
                    text(
                        "SELECT id, stock_units, acquired_at FROM products "
                        "WHERE tenant_id = :tid AND name = :n"
                    ),
                    {"tid": tid, "n": name},
                )
            ).first()
            if prod is None:
                print(f"\n=== {name}: NO ENCONTRADO ===")
                continue
            pid, stock_units, acquired_at = prod

            print(f"\n=== {name} (stock_units actual = {stock_units}) ===")

            by_type = (
                await session.execute(
                    text(
                        "SELECT movement_type, COUNT(*) AS n, SUM(qty) AS total_qty, "
                        "       MIN(created_at) AS first_at, MAX(created_at) AS last_at "
                        "FROM inventory_movements "
                        "WHERE tenant_id = :tid AND product_id = :pid AND voided_at IS NULL "
                        "GROUP BY movement_type"
                    ),
                    {"tid": tid, "pid": pid},
                )
            ).mappings().all()
            print("  inventory_movements vivos por tipo:")
            ledger_sum = 0
            for r in by_type:
                print(
                    f"    {r['movement_type']}: n={r['n']} Σqty={r['total_qty']} "
                    f"[{r['first_at']} .. {r['last_at']}]"
                )
                ledger_sum += int(r["total_qty"])
            print(f"  Σ TOTAL de movimientos vivos: {ledger_sum}")

            sales = (
                await session.execute(
                    text(
                        "SELECT COUNT(*) AS n, COALESCE(SUM(quantity), 0) AS total_qty, "
                        "       MIN(transaction_date) AS first_at, "
                        "       MAX(transaction_date) AS last_at "
                        "FROM sales_entries "
                        "WHERE tenant_id = :tid AND product_id = :pid AND voided_at IS NULL"
                    ),
                    {"tid": tid, "pid": pid},
                )
            ).mappings().one()
            print(
                f"  ventas vivas (sales_entries): n={sales['n']} "
                f"Σquantity={sales['total_qty']} "
                f"[{sales['first_at']} .. {sales['last_at']}]"
            )
            print(f"  acquired_at (fecha alta catálogo): {acquired_at}")
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
