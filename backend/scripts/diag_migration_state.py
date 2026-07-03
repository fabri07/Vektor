"""Diagnóstico read-only del estado de la migración 20260727_0001 en Neon.

Usage:
    DATABASE_URL='postgresql://...neon.tech/vektor?sslmode=require' \
        .venv/bin/python scripts/diag_migration_state.py

ONLY runs SELECT statements. No writes. Safe against production.
"""

import asyncio
import os
import sys

import asyncpg
from _db import normalize_dsn

EXPECTED_COLUMNS = {
    "source_type",
    "source_upload_id",
    "source_row_ref",
    "source_row_hash",
    "voided_at",
}


async def main() -> None:
    raw = os.environ.get("DATABASE_URL") or os.environ.get("DATABASE_URL_SYNC")
    if not raw:
        print("ERROR: exportá DATABASE_URL con la URL de Neon antes de correr.")
        sys.exit(2)
    dsn, sslarg = normalize_dsn(raw)
    conn = await asyncpg.connect(dsn, ssl=sslarg)
    try:
        version_rows = await conn.fetch("SELECT version_num FROM alembic_version")
        print("alembic_version:", [r["version_num"] for r in version_rows])

        cols = await conn.fetch(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'inventory_movements'"
        )
        present = {r["column_name"] for r in cols}
        print("\nColumnas de inventory_movements relevantes a 20260727_0001:")
        for col in sorted(EXPECTED_COLUMNS):
            print(f"  {'OK ' if col in present else 'FALTA '} {col}")

        fk = await conn.fetch(
            "SELECT conname FROM pg_constraint WHERE conname = "
            "'fk_inventory_movements_source_upload'"
        )
        print(
            "\nFK fk_inventory_movements_source_upload:",
            "OK" if fk else "FALTA",
        )

        idx = await conn.fetch(
            "SELECT indexname FROM pg_indexes WHERE tablename = 'inventory_movements' "
            "AND indexname IN ("
            "'ix_inventory_movements_source_upload_id', "
            "'ix_inventory_movements_source_row_hash')"
        )
        idx_names = {r["indexname"] for r in idx}
        for name in (
            "ix_inventory_movements_source_upload_id",
            "ix_inventory_movements_source_row_hash",
        ):
            print(f"Índice {name}:", "OK" if name in idx_names else "FALTA")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
