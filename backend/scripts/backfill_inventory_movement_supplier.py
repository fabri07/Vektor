"""Backfill de inventory_movements.supplier_id para compras previas (Fase 3).

La columna ``inventory_movements.supplier_id`` (la agrega la migración
``20260720_0004``) queda NULL para todos los movimientos de compra anteriores al
deploy. Este script la puebla cruzando cada movimiento ``movement_type='purchase'``
con el ``expense_entries`` correspondiente (mismo tenant, mismo product_id, misma
fecha calendario, supplier_id no nulo). Así el detalle del proveedor no se ve vacío
para proveedores ya existentes.

CRITERIO DE MATCH (por movimiento purchase con supplier_id IS NULL):
    Buscar expense_entries del MISMO tenant con:
        product_id = movimiento.product_id
        date(transaction_date) = date(movimiento.created_at)
        supplier_id IS NOT NULL
        voided_at IS NULL
    Desempate si hay varios candidatos: el de monto más cercano a
    (qty * unit_cost) del movimiento. Si tras el desempate sigue habiendo más de
    un supplier_id distinto → AMBIGUO, se deja NULL.

RESULTADOS (contados y reportados en --out):
    INFERRED  → match único razonable → se setea ese supplier_id (solo con --apply).
    NO_EXPENSE → no hay gasto del mismo product_id/fecha → queda NULL.
    NO_SUPPLIER → hay gasto pero sin supplier_id → queda NULL.
    AMBIGUOUS → varios gastos con supplier_id distinto → queda NULL.
    ALREADY_SET → el movimiento ya tenía supplier_id → no se toca.

Usage:
    # Dry-run (default) de un tenant: cuenta qué se podría inferir, no escribe.
    DATABASE_URL='postgresql://...' .venv/bin/python \
        scripts/backfill_inventory_movement_supplier.py --tenant <uuid>

    # Dry-run global:
    ... scripts/backfill_inventory_movement_supplier.py --all-active

    # Reporte detallado a archivo (CSV por extensión .csv, si no JSON):
    ... scripts/backfill_inventory_movement_supplier.py --tenant <uuid> --out reporte.csv

    # Aplicar (después de revisar el dry-run):
    ... scripts/backfill_inventory_movement_supplier.py --tenant <uuid> --apply

REVERSIBLE: el undo es limpiar lo que este backfill seteó. Como cada cambio queda
auditado en decision_audit_log (decision_type='INVENTORY_SUPPLIER_BACKFILL', con
movement_id + supplier_id seteado), se puede revertir por traza o en bloque:

    UPDATE inventory_movements SET supplier_id = NULL
      WHERE id IN (
        SELECT (decision_data->>'movement_id')::uuid
        FROM decision_audit_log
        WHERE tenant_id = '<tid>' AND decision_type = 'INVENTORY_SUPPLIER_BACKFILL'
      );

NUNCA imprime la connection URL. Read-only por default. Correr desde backend/.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import sys
import uuid
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _db import async_engine_config  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine  # noqa: E402

_DECISION_TYPE = "INVENTORY_SUPPLIER_BACKFILL"

_INFERRED = "INFERRED"
_NO_EXPENSE = "NO_EXPENSE"
_NO_SUPPLIER = "NO_SUPPLIER"
_AMBIGUOUS = "AMBIGUOUS"


async def _plan_tenant(session: AsyncSession, tid: uuid.UUID) -> list[dict[str, Any]]:
    """Decide, sin escribir, qué supplier_id correspondería a cada movimiento purchase.

    Devuelve una fila por movimiento candidato (los que tienen supplier_id NULL).
    """
    movements = (
        await session.execute(
            text(
                "SELECT id, product_id, qty, unit_cost, created_at "
                "FROM inventory_movements "
                "WHERE tenant_id = :tid AND movement_type = 'purchase' "
                "AND supplier_id IS NULL "
                "ORDER BY created_at"
            ),
            {"tid": tid},
        )
    ).mappings().all()

    rows: list[dict[str, Any]] = []
    for m in movements:
        target_amount = None
        if m["unit_cost"] is not None and m["qty"] is not None:
            target_amount = abs(m["qty"]) * float(m["unit_cost"])

        # Gastos del mismo producto + misma fecha calendario, con supplier.
        candidates = (
            await session.execute(
                text(
                    "SELECT supplier_id, amount FROM expense_entries "
                    "WHERE tenant_id = :tid AND product_id = :pid "
                    "AND voided_at IS NULL "
                    "AND date(transaction_date) = date(:created)"
                ),
                {"tid": tid, "pid": m["product_id"], "created": m["created_at"]},
            )
        ).mappings().all()

        with_supplier = [c for c in candidates if c["supplier_id"] is not None]

        if not candidates:
            reason, supplier_id = _NO_EXPENSE, None
        elif not with_supplier:
            reason, supplier_id = _NO_SUPPLIER, None
        else:
            distinct = {c["supplier_id"] for c in with_supplier}
            if len(distinct) == 1:
                reason, supplier_id = _INFERRED, next(iter(distinct))
            elif target_amount is not None:
                # Solo inferir con un match EXACTO (no "el más cercano"): el gasto COGS
                # de la compra suele tener amount == qty*unit_cost. Si ningún gasto
                # coincide con el target, o coinciden VARIOS proveedores, es AMBIGUOUS
                # (no adivinar — elegir "el más cercano" puede atribuir el proveedor
                # equivocado cuando hay un gasto no relacionado con monto parecido).
                exact = {
                    c["supplier_id"]
                    for c in with_supplier
                    if abs(float(c["amount"]) - target_amount) < 0.01
                }
                if len(exact) == 1:
                    reason, supplier_id = _INFERRED, next(iter(exact))
                else:
                    reason, supplier_id = _AMBIGUOUS, None
            else:
                reason, supplier_id = _AMBIGUOUS, None

        rows.append(
            {
                "tenant_id": str(tid),
                "movement_id": str(m["id"]),
                "product_id": str(m["product_id"]),
                "reason": reason,
                "supplier_id": str(supplier_id) if supplier_id else None,
            }
        )
    return rows


async def _apply_tenant(
    session: AsyncSession, tid: uuid.UUID, plan: list[dict[str, Any]]
) -> int:
    """Setea supplier_id en los movimientos INFERRED. Auditado y reversible."""
    applied = 0
    for r in plan:
        if r["reason"] != _INFERRED or not r["supplier_id"]:
            continue
        mid = uuid.UUID(r["movement_id"])
        sid = uuid.UUID(r["supplier_id"])
        await session.execute(
            text(
                "UPDATE inventory_movements SET supplier_id = :sid "
                "WHERE id = :mid AND tenant_id = :tid AND supplier_id IS NULL"
            ),
            {"sid": sid, "mid": mid, "tid": tid},
        )
        decision_data = {
            "movement_id": str(mid),
            "product_id": r["product_id"],
            "supplier_id": str(sid),
            "before": {"supplier_id": None},
            "after": {"supplier_id": str(sid)},
        }
        await session.execute(
            text(
                "INSERT INTO decision_audit_log "
                "(id, tenant_id, decision_type, decision_data, triggered_by, created_at) "
                "VALUES (gen_random_uuid(), :tid, :dt, CAST(:dd AS jsonb), :tb, now())"
            ),
            {
                "tid": tid,
                "dt": _DECISION_TYPE,
                "dd": json.dumps(decision_data),
                "tb": "script:backfill_inventory_movement_supplier",
            },
        )
        applied += 1
    return applied


def _print_summary(tid: uuid.UUID, plan: list[dict[str, Any]]) -> None:
    from collections import Counter

    by_reason = Counter(r["reason"] for r in plan)
    print(
        f"tenant {tid}: {len(plan)} movimiento(s) purchase sin supplier — {dict(by_reason)}"
    )


def _write_report(path: str, rows: list[dict[str, Any]]) -> None:
    if path.lower().endswith(".csv"):
        with open(path, "w", newline="", encoding="utf-8-sig") as fh:
            writer = csv.DictWriter(
                fh,
                fieldnames=["tenant_id", "movement_id", "product_id", "reason", "supplier_id"],
            )
            writer.writeheader()
            writer.writerows(rows)
    else:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(rows, fh, ensure_ascii=False, indent=2)
    print(f"\nReporte escrito en {path} ({len(rows)} fila(s)).")


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant", help="UUID de tenant puntual")
    parser.add_argument("--all-active", action="store_true", help="Todos los tenants activos")
    parser.add_argument("--apply", action="store_true", help="Escribir cambios (default: dry-run)")
    parser.add_argument("--out", help="Path del reporte (CSV si .csv, si no JSON)")
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
        print(f"[{mode}] backfill supplier_id en inventory_movements de {len(tids)} tenant(s)\n")

        all_rows: list[dict[str, Any]] = []
        total_applied = 0
        for tid in tids:
            plan = await _plan_tenant(session, tid)
            all_rows.extend(plan)
            _print_summary(tid, plan)
            if args.apply:
                total_applied += await _apply_tenant(session, tid, plan)
            print()

        inferred = sum(1 for r in all_rows if r["reason"] == _INFERRED)
        left_null = len(all_rows) - inferred
        print(
            f"TOTAL: {inferred} inferible(s) | {left_null} quedan NULL "
            f"(sin gasto / sin supplier / ambiguo)"
        )

        if args.out and all_rows:
            _write_report(args.out, all_rows)

        if args.apply:
            await session.commit()
            print(
                f"COMMIT: {total_applied} movimiento(s) actualizados "
                f"(decision_type={_DECISION_TYPE}). Reversible: ver docstring."
            )
        else:
            await session.rollback()
            print(f"Dry-run: nada se escribió. {inferred} se setearían con --apply.")
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
