"""Chequeo read-only rápido de stock_units actual para productos puntuales.

Usage:
    DATABASE_URL='...' .venv/bin/python scripts/diag_stock_outliers.py \
        --tenant <uuid> --product "Nombre exacto" [--product "Otro nombre" ...]

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
            row = (
                await session.execute(
                    text(
                        "SELECT name, stock_units, unit_cost_ars "
                        "FROM products WHERE tenant_id = :tid AND name = :n"
                    ),
                    {"tid": tid, "n": name},
                )
            ).first()
            print(dict(row._mapping) if row else f"(no encontrado: {name})")
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
