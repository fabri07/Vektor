"""Backfill: ventas históricas sin cliente → cliente sentinela "Local" (Reforma Clientes).

La reforma de Clientes introduce el invariante "ninguna venta queda sin cliente":
las ventas sin cliente registrado se agrupan en un único cliente **"Local"** por
tenant (``custom_fields->>'_sentinel' = 'true'``, garantizado por el índice único
parcial de la migración ``20260721_0001``). Todas las ventas anteriores al deploy
tienen ``customer_id IS NULL``; este script las reasigna a "Local".

CRITERIO (por tenant):
    1. get-or-create del sentinela "Local" (busca por flag, no por nombre).
    2. UPDATE sales_entries SET customer_id = <local> WHERE customer_id IS NULL
       AND voided_at IS NULL.

    El fiado (``payment_method='account'``) histórico SÍ se reasigna a "Local" en el
    backfill: son datos viejos sin cliente mapeado y el invariante de no-NULL manda.
    La regla "fiado exige cliente real" aplica de acá en adelante al alta interactiva.

RESULTADOS:
    REASSIGNED  → ventas movidas a "Local" (solo con --apply).
    SENTINEL_CREATED / SENTINEL_REUSED → si hubo que crear el "Local" o ya existía.

Usage:
    # Dry-run (default) de un tenant: cuenta cuántas ventas se reasignarían.
    DATABASE_URL='postgresql://...' .venv/bin/python \
        scripts/backfill_local_customer.py --tenant <uuid>

    # Dry-run global:
    ... scripts/backfill_local_customer.py --all-active

    # Aplicar (después de revisar el dry-run):
    ... scripts/backfill_local_customer.py --tenant <uuid> --apply

REVERSIBLE: cada reasignación queda auditada en decision_audit_log
(decision_type='LOCAL_CUSTOMER_BACKFILL', con sale_id). Revertir por traza:

    UPDATE sales_entries SET customer_id = NULL
      WHERE id IN (
        SELECT (decision_data->>'sale_id')::uuid
        FROM decision_audit_log
        WHERE tenant_id = '<tid>' AND decision_type = 'LOCAL_CUSTOMER_BACKFILL'
      );

NUNCA imprime la connection URL. Read-only por default. Correr desde backend/.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _db import async_engine_config  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine  # noqa: E402

_DECISION_TYPE = "LOCAL_CUSTOMER_BACKFILL"
_LOCAL_NAME = "Local"


async def _orphan_count(session: AsyncSession, tid: uuid.UUID) -> int:
    row = await session.execute(
        text(
            "SELECT count(*) FROM sales_entries "
            "WHERE tenant_id = :tid AND customer_id IS NULL AND voided_at IS NULL"
        ),
        {"tid": tid},
    )
    return int(row.scalar_one() or 0)


async def _resolve_local(session: AsyncSession, tid: uuid.UUID) -> tuple[uuid.UUID, bool]:
    """get-or-create del sentinela "Local". Devuelve (id, created)."""
    existing = await session.execute(
        text(
            "SELECT id FROM customers "
            "WHERE tenant_id = :tid AND deactivated_at IS NULL "
            "AND custom_fields->>'_sentinel' = 'true' LIMIT 1"
        ),
        {"tid": tid},
    )
    found = existing.scalar_one_or_none()
    if found is not None:
        return found, False
    new_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO customers (id, tenant_id, name, custom_fields, created_at, updated_at) "
            "VALUES (:id, :tid, :name, CAST(:cf AS jsonb), now(), now())"
        ),
        {"id": new_id, "tid": tid, "name": _LOCAL_NAME, "cf": json.dumps({"_sentinel": "true"})},
    )
    return new_id, True


async def _apply_tenant(session: AsyncSession, tid: uuid.UUID, local_id: uuid.UUID) -> int:
    """Audita por venta y reasigna en bloque. Reversible (ver docstring)."""
    orphans = (
        await session.execute(
            text(
                "SELECT id FROM sales_entries "
                "WHERE tenant_id = :tid AND customer_id IS NULL AND voided_at IS NULL"
            ),
            {"tid": tid},
        )
    ).scalars().all()
    for sid in orphans:
        await session.execute(
            text(
                "INSERT INTO decision_audit_log "
                "(id, tenant_id, decision_type, decision_data, triggered_by, created_at) "
                "VALUES (gen_random_uuid(), :tid, :dt, CAST(:dd AS jsonb), :tb, now())"
            ),
            {
                "tid": tid,
                "dt": _DECISION_TYPE,
                "dd": json.dumps(
                    {
                        "sale_id": str(sid),
                        "before": {"customer_id": None},
                        "after": {"customer_id": str(local_id)},
                    }
                ),
                "tb": "script:backfill_local_customer",
            },
        )
    result = await session.execute(
        text(
            "UPDATE sales_entries SET customer_id = :cid "
            "WHERE tenant_id = :tid AND customer_id IS NULL AND voided_at IS NULL"
        ),
        {"cid": local_id, "tid": tid},
    )
    return int(result.rowcount or 0)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant", help="UUID de tenant puntual")
    parser.add_argument("--all-active", action="store_true", help="Todos los tenants activos")
    parser.add_argument("--apply", action="store_true", help="Escribir cambios (default: dry-run)")
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

        mode = "APPLY" if args.apply else "DRY-RUN"
        print(f"[{mode}] backfill ventas sin cliente → 'Local' en {len(tids)} tenant(s)\n")

        total_orphans = 0
        total_applied = 0
        for tid in tids:
            orphans = await _orphan_count(session, tid)
            total_orphans += orphans
            print(f"tenant {tid}: {orphans} venta(s) sin cliente")
            if args.apply and orphans:
                local_id, created = await _resolve_local(session, tid)
                print(f"  'Local' {'creado' if created else 'reusado'}: {local_id}")
                total_applied += await _apply_tenant(session, tid, local_id)

        print(f"\nTOTAL: {total_orphans} venta(s) sin cliente")
        if args.apply:
            await session.commit()
            print(
                f"COMMIT: {total_applied} venta(s) reasignadas a 'Local' "
                f"(decision_type={_DECISION_TYPE}). Reversible: ver docstring."
            )
        else:
            await session.rollback()
            print(f"Dry-run: nada se escribió. {total_orphans} se reasignarían con --apply.")
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
