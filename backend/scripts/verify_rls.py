"""Verifica que todas las tablas críticas tienen Row Level Security (RLS) activado.

Uso (requiere DATABASE_URL apuntando a PostgreSQL):
    DATABASE_URL=postgresql://... python scripts/verify_rls.py

Solo ejecutar contra la DB de producción o staging — no aplica a SQLite de tests.
"""

from __future__ import annotations

import asyncio
import os
import sys

REQUIRED_TABLES = [
    "sales",
    "cash_movements",
    "inventory_movements",
    "inventory_balances",
    "products",
    "suppliers",
    "purchase_orders",
    "file_assets",
    "gmail_messages",
    "health_snapshots",
    "events",
    "audit_logs",
    "pending_actions",
]


async def verify_rls() -> None:
    try:
        import asyncpg  # noqa: PLC0415
    except ImportError:
        print("ERROR: asyncpg no está instalado. Ejecutar: pip install asyncpg")
        sys.exit(1)

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("ERROR: Variable DATABASE_URL no definida.")
        sys.exit(1)

    conn = await asyncpg.connect(database_url)
    try:
        rows = await conn.fetch(
            "SELECT tablename FROM pg_tables "
            "WHERE rowsecurity = true AND schemaname = 'public'"
        )
        protected = {r["tablename"] for r in rows}
        missing = [t for t in REQUIRED_TABLES if t not in protected]

        if missing:
            print(f"FALTA RLS EN: {missing}")
            sys.exit(1)
        else:
            print(f"RLS OK en {len(protected)} tablas protegidas.")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(verify_rls())
