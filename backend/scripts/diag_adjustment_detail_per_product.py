"""Lista CADA movimiento 'adjustment' vivo de un producto, individualmente (no
agregado), para distinguir un ajuste grande sospechoso (posible alta de stock con
signo invertido) de mermas chicas legítimas.

Usage:
    DATABASE_URL='...' .venv/bin/python scripts/diag_adjustment_detail_per_product.py \
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
                        "SELECT id, stock_units FROM products "
                        "WHERE tenant_id = :tid AND name = :n"
                    ),
                    {"tid": tid, "n": name},
                )
            ).first()
            if prod is None:
                print(f"\n=== {name}: NO ENCONTRADO ===")
                continue
            pid, stock_units = prod
            print(f"\n=== {name} (stock_units actual = {stock_units}) ===")

            rows = (
                await session.execute(
                    text(
                        "SELECT qty, unit_cost, created_at, source_type, source_upload_id "
                        "FROM inventory_movements "
                        "WHERE tenant_id = :tid AND product_id = :pid "
                        "AND movement_type = 'adjustment' AND voided_at IS NULL "
                        "ORDER BY created_at"
                    ),
                    {"tid": tid, "pid": pid},
                )
            ).mappings().all()
            for r in rows:
                print(f"  qty={r['qty']:>6}  {r['created_at']}  unit_cost={r['unit_cost']}")
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
