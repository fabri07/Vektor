"""Backfill de inventory_movements.supplier_id para stock de CATÁLOGO (Bloque 2).

Hallazgo ASTERIA 2026-09-13: `_apply_catalog_stock` (ingestion_import_service.py)
nunca recibía el `supplier_id` ya resuelto por `_declarar_link_proveedor`, así que
todo movimiento de stock nacido de un catálogo ("Tienda" mapeada a Proveedor)
quedaba con `supplier_id=NULL` aunque el vínculo Producto↔Proveedor ya existiera en
`product_supplier_links`. El fix (ver commit `cf1599b4`) cubre las importaciones
NUEVAS; este script completa las que ya quedaron escritas con el bug.

CRITERIO DE MATCH (por movimiento con source_type='catalog_initial_stock' y
supplier_id IS NULL):
    Buscar en `product_supplier_links` los vínculos ACTIVOS (voided_at IS NULL)
    del MISMO product_id.
        0 vínculos  → NO_LINK   (el catálogo no declaró proveedor para ese producto
                                  — nada que inferir, queda NULL)
        1 vínculo   → INFERRED  → se setea ese supplier_id (solo con --apply)
        2+ vínculos → AMBIGUOUS (el producto se declaró comprado en más de una
                                  tienda; sin releer el archivo no se puede saber
                                  a cuál de las dos corresponde ESTE movimiento en
                                  particular — no se adivina, queda NULL)

Deliberadamente NO toca movimientos con otro `source_type` (compras vía libro de
gastos): esos ya los cubre `backfill_inventory_movement_supplier.py`, que infiere
por gasto asociado en vez de por `product_supplier_links` — universos distintos,
sin superposición (un movimiento solo tiene un `source_type`).

RESULTADOS (contados y reportados en --out):
    INFERRED / NO_LINK / AMBIGUOUS / ya no aparecen si supplier_id ya estaba seteado.

Usage:
    # Dry-run (default) de un tenant:
    DATABASE_URL='postgresql://...' .venv/bin/python \
        scripts/backfill_catalog_stock_supplier_from_link.py --tenant <uuid>

    # Dry-run global:
    ... scripts/backfill_catalog_stock_supplier_from_link.py --all-active

    # Reporte detallado a archivo (CSV por extensión .csv, si no JSON):
    ... scripts/backfill_catalog_stock_supplier_from_link.py --tenant <uuid> --out reporte.csv

    # Aplicar (después de revisar el dry-run):
    ... scripts/backfill_catalog_stock_supplier_from_link.py --tenant <uuid> --apply

REVERSIBLE: cada cambio queda auditado en decision_audit_log
(decision_type='CATALOG_STOCK_SUPPLIER_BACKFILL', con movement_id + supplier_id
seteado). Revertir por traza:

    UPDATE inventory_movements SET supplier_id = NULL
      WHERE id IN (
        SELECT (decision_data->>'movement_id')::uuid
        FROM decision_audit_log
        WHERE tenant_id = '<tid>' AND decision_type = 'CATALOG_STOCK_SUPPLIER_BACKFILL'
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

_DECISION_TYPE = "CATALOG_STOCK_SUPPLIER_BACKFILL"
_SOURCE_TYPE = "catalog_initial_stock"

_INFERRED = "INFERRED"
_NO_LINK = "NO_LINK"
_AMBIGUOUS = "AMBIGUOUS"


async def _plan_tenant(session: AsyncSession, tid: uuid.UUID) -> list[dict[str, Any]]:
    """Decide, sin escribir, qué supplier_id correspondería a cada movimiento."""
    movements = (
        await session.execute(
            text(
                "SELECT id, product_id FROM inventory_movements "
                "WHERE tenant_id = :tid AND source_type = :st "
                "AND supplier_id IS NULL "
                "ORDER BY created_at"
            ),
            {"tid": tid, "st": _SOURCE_TYPE},
        )
    ).mappings().all()

    rows: list[dict[str, Any]] = []
    for m in movements:
        links = (
            await session.execute(
                text(
                    "SELECT DISTINCT supplier_id FROM product_supplier_links "
                    "WHERE tenant_id = :tid AND product_id = :pid AND voided_at IS NULL"
                ),
                {"tid": tid, "pid": m["product_id"]},
            )
        ).scalars().all()

        if not links:
            reason, supplier_id = _NO_LINK, None
        elif len(links) == 1:
            reason, supplier_id = _INFERRED, links[0]
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
                "tb": "script:backfill_catalog_stock_supplier_from_link",
            },
        )
        applied += 1
    return applied


def _print_summary(tid: uuid.UUID, plan: list[dict[str, Any]]) -> None:
    from collections import Counter

    by_reason = Counter(r["reason"] for r in plan)
    print(
        f"tenant {tid}: {len(plan)} movimiento(s) de catálogo sin supplier — "
        f"{dict(by_reason)}"
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
        print(
            f"[{mode}] backfill supplier_id de stock de catálogo en "
            f"{len(tids)} tenant(s)\n"
        )

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
            f"(sin vínculo declarado / ambiguo)"
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
