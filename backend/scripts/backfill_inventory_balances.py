"""Backfill de inventory_balances faltantes desde products.stock_units.

Usage:
    # Dry-run (default): muestra qué crearía, no escribe nada.
    DATABASE_URL='postgresql://...' .venv/bin/python scripts/backfill_inventory_balances.py

    # Aplicar a un tenant puntual (piloto) o a todos:
    ... scripts/backfill_inventory_balances.py --apply --tenant <uuid>
    ... scripts/backfill_inventory_balances.py --apply --all

Qué hace (por tenant): crea un InventoryBalance (current_qty = stock_units,
reserved_qty = 0) para cada producto ACTIVO que no tenga balance. No toca
balances existentes (aunque difieran de products.stock_units: products es la
representación canónica que lee la UI; un balance existente puede tener
reservas en vuelo). Idempotente — correrlo dos veces no duplica.

NUNCA imprime la connection URL. Requiere correr desde backend/.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _db import async_engine_config  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine  # noqa: E402


async def _tenant_ids(session: AsyncSession, tenant: str | None) -> list[uuid.UUID]:
    if tenant:
        return [uuid.UUID(tenant)]
    rows = await session.execute(text("SELECT tenant_id FROM tenants ORDER BY created_at"))
    return [r[0] for r in rows.all()]


async def backfill_balances(session: AsyncSession, tid: uuid.UUID, apply: bool) -> None:
    rows = (
        await session.execute(
            text(
                "SELECT p.id, p.name, p.stock_units FROM products p "
                "LEFT JOIN inventory_balances b "
                "ON b.product_id = p.id AND b.tenant_id = p.tenant_id "
                "WHERE p.tenant_id = :tid AND p.is_active = true AND b.id IS NULL "
                "ORDER BY p.name"
            ),
            {"tid": tid},
        )
    ).all()
    print(f"    productos activos sin balance: {len(rows)}")
    for pid, name, stock in rows:
        print(f"      {str(name)[:40]:40} stock_units={stock}")
        if apply:
            await session.execute(
                text(
                    "INSERT INTO inventory_balances "
                    "(id, tenant_id, product_id, current_qty, reserved_qty, "
                    " created_at, updated_at) "
                    "VALUES (gen_random_uuid(), :tid, :pid, :qty, 0, now(), now())"
                ),
                {"tid": tid, "pid": pid, "qty": stock or 0},
            )


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Escribir cambios (default: dry-run)")
    parser.add_argument("--tenant", help="UUID de tenant puntual")
    parser.add_argument("--all", action="store_true", help="Todos los tenants")
    args = parser.parse_args()

    if args.apply and not (args.tenant or args.all):
        print("ERROR: --apply requiere --tenant <uuid> o --all explícito.")
        sys.exit(2)

    url, connect_args = async_engine_config()
    engine = create_async_engine(url, connect_args=connect_args)
    async with AsyncSession(engine) as session:
        tids = await _tenant_ids(session, args.tenant)
        mode = "APPLY" if args.apply else "DRY-RUN"
        print(f"[{mode}] {len(tids)} tenant(s)")
        for tid in tids:
            print(f"\n  tenant {tid}")
            await backfill_balances(session, tid, args.apply)
        if args.apply:
            await session.commit()
            print("\nCOMMIT aplicado.")
        else:
            await session.rollback()
            print("\nDry-run: nada se escribió.")
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
