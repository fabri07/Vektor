"""Revierte una corrida de ``reconcile_untagged_adjustments.py``.

FUENTE DE VERDAD: ``decision_audit_log`` (decision_type='UNTAGGED_ADJUSTMENT_RECONCILIATION').
El ``decision_data`` de la corrida guarda los ``items`` (backfill/void por movimiento) y
los ``stock_changes`` (before/after ABSOLUTO por producto). Con eso se deshace todo.

QUÉ HACE POR CADA ITEM de la corrida:
    voided: true   → reactiva el movimiento (``voided_at = NULL``) y restaura el stock
        del producto al ``stock_before`` ABSOLUTO auditado (no incremental — porque el
        apply pudo clampar a 0). Restaura ``stock_units`` y ``inventory_balances.current_qty``.
    backfill       → vuelve ``source_type = NULL`` (y limpia ``source_upload_id`` /
        ``source_row_ref`` SOLO si la corrida los seteó — caso catalog).

CORRIDAS ``--void-keep-stock`` (``void_stock_mode: "keep"``): el apply NO tocó stock y
grabó ``stock_changes: []`` a propósito — esta reversa entonces solo des-voidea los
movimientos y NO restaura stock (el paso 3 no encuentra ``stock_before`` para ningún
producto). Es el comportamiento correcto: no hay nada de stock que deshacer.

⚠ ABORTA SI EL CHECK YA ESTÁ APLICADO. La constraint
``ck_inventory_movements_adjustment_source_type`` (migración 20260728_0001) exige que
todo adjustment VIVO tenga ``source_type``. Des-taggear (source_type→NULL) o des-voidear
(voided_at→NULL) un adjustment sin procedencia lo dejaría vivo + sin source_type y
violaría la constraint. Por eso esta reversa SOLO es segura ANTES de aplicar el CHECK; si
lo detecta creado, aborta con un mensaje claro (la reversa post-CHECK exige voidear en vez
de des-taggear, decisión que queda para el operador).

Todo dry-run por defecto; ``--apply`` escribe. NUNCA imprime la connection URL. Correr
desde ``backend/``.

Usage:
    # Revertir una corrida puntual (dry-run)
    DATABASE_URL='postgresql://...' .venv/bin/python \
        scripts/revert_untagged_adjustment_reconciliation.py --audit-id <uuid>

    # Revertir TODAS las corridas de un tenant (dry-run), luego --apply
    ... --tenant <uuid>
    ... --tenant <uuid> --apply
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _db import async_engine_config, insert_decision_audit  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine  # noqa: E402

_DECISION_TYPE = "UNTAGGED_ADJUSTMENT_RECONCILIATION"
_REVERT_DECISION = "UNTAGGED_ADJUSTMENT_RECONCILIATION_REVERT"
_TRIGGERED_BY = "script:revert_untagged_adjustment_reconciliation"

# CHECK que hace insegura la reversa post-aplicación (migración 20260728_0001).
_CHECK_CONSTRAINT = "ck_inventory_movements_adjustment_source_type"

# source_type que el apply pudo backfillear (para el WHERE idempotente al des-taggear).
_SOURCE_CATALOG_INITIAL_STOCK = "catalog_initial_stock"
_SOURCE_RECONCILIATION = "reconciliation"


def _as_dict(value: Any) -> dict[str, Any] | None:
    """decision_data puede venir como dict (asyncpg JSONB) o str JSON. Defensivo: un
    JSON corrupto/inesperado devuelve None (el caller saltea la fila) en vez de
    crashear a mitad de la reversa (mismo patrón que detect_misvoided_purchases.py)."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (ValueError, TypeError):
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


async def _check_constraint_applied(session: AsyncSession) -> bool:
    """True si el CHECK ya existe en la DB (reversa insegura → abortar)."""
    row = (
        await session.execute(
            text("SELECT 1 FROM pg_constraint WHERE conname = :cn LIMIT 1"),
            {"cn": _CHECK_CONSTRAINT},
        )
    ).first()
    return row is not None


async def _load_runs(
    session: AsyncSession, *, audit_id: str | None, tid: str | None
) -> list[dict[str, Any]]:
    """Corridas a revertir (read-only). Por --audit-id (una) o --tenant (todas).

    ORDER BY created_at DESC (newest-first) a propósito: cada corrida restaura
    ``stock_before`` ABSOLUTO por producto. Si dos corridas tocaron el mismo producto,
    revertir oldest-first dejaría el stock en el estado intermedio (el S0 de la corrida
    vieja quedaría pisado por el de la nueva). Newest-first deshace primero la última
    corrida y termina aplicando el ``stock_before`` de la más vieja — el S0 verdadero
    previo a toda reconciliación.
    """
    if audit_id:
        rows = (
            await session.execute(
                text(
                    "SELECT id, tenant_id, decision_data FROM decision_audit_log "
                    "WHERE id = CAST(:aid AS uuid) AND decision_type = :dt"
                ),
                {"aid": audit_id, "dt": _DECISION_TYPE},
            )
        ).mappings().all()
    else:
        rows = (
            await session.execute(
                text(
                    "SELECT id, tenant_id, decision_data FROM decision_audit_log "
                    "WHERE tenant_id = CAST(:tid AS uuid) AND decision_type = :dt "
                    # DESC: revertir newest-first para que el stock_before ABSOLUTO de la
                    # corrida MÁS VIEJA (el S0 verdadero) sea el que pise último por
                    # producto (ver docstring). ASC dejaría el estado intermedio.
                    "ORDER BY created_at DESC"
                ),
                {"tid": tid, "dt": _DECISION_TYPE},
            )
        ).mappings().all()
    runs: list[dict[str, Any]] = []
    skipped = 0
    for r in rows:
        dd = _as_dict(r["decision_data"])
        if dd is None:
            skipped += 1
            print(
                f"  ⚠ SALTEADA corrida audit={r['id']}: decision_data ilegible "
                "(JSON corrupto/inesperado) — no se puede revertir con seguridad."
            )
            continue
        runs.append(
            {
                "audit_id": str(r["id"]),
                "tenant_id": str(r["tenant_id"]),
                "decision_data": dd,
            }
        )
    if skipped:
        print(f"  ({skipped} corrida(s) salteada(s) por decision_data ilegible)")
    return runs


async def _revert_run(session: AsyncSession, run: dict[str, Any]) -> dict[str, Any]:
    """Deshace una corrida. Reversible y auditado. Idempotente por WHERE."""
    tid = run["tenant_id"]
    dd = run["decision_data"]
    items = dd.get("items") or []
    stock_changes = dd.get("stock_changes") or []
    stock_before_by_product = {sc["product_id"]: sc["stock_before"] for sc in stock_changes}

    unvoided = 0
    untagged = 0
    skipped_untag = 0
    skipped_unvoid = 0
    products_restored: list[str] = []

    # 1. Des-taggear backfills (source_type → NULL; limpiar upload_id/row_ref si catalog).
    for it in items:
        bf = it.get("backfill")
        if not bf:
            continue
        st = bf.get("source_type")
        if bf.get("source_upload_id") is not None:
            # Caso catalog: limpiar los tres campos que la corrida seteó.
            result = await session.execute(
                text(
                    "UPDATE inventory_movements "
                    "SET source_type = NULL, source_upload_id = NULL, source_row_ref = NULL "
                    "WHERE id = CAST(:mid AS uuid) AND tenant_id = CAST(:tid AS uuid) "
                    "AND source_type = :st"
                ),
                {"mid": it["movement_id"], "tid": tid, "st": st},
            )
        else:
            # Caso reconciliation: solo tocó source_type.
            result = await session.execute(
                text(
                    "UPDATE inventory_movements SET source_type = NULL "
                    "WHERE id = CAST(:mid AS uuid) AND tenant_id = CAST(:tid AS uuid) "
                    "AND source_type = :st"
                ),
                {"mid": it["movement_id"], "tid": tid, "st": st},
            )
        # rowcount==0 → ya estaba des-tagueado (corrida ya revertida antes): no-op,
        # no cuenta como mutación real.
        if result.rowcount:
            untagged += 1
        else:
            skipped_untag += 1

    # 2. Des-voidear movimientos.
    products_to_restore: set[str] = set()
    for it in items:
        if not it.get("voided"):
            continue
        result = await session.execute(
            text(
                "UPDATE inventory_movements SET voided_at = NULL "
                "WHERE id = CAST(:mid AS uuid) AND tenant_id = CAST(:tid AS uuid) "
                "AND voided_at IS NOT NULL"
            ),
            {"mid": it["movement_id"], "tid": tid},
        )
        # rowcount==0 → ya estaba des-voideado (no-op): no cuenta, y no re-dispara la
        # restauración de stock del producto (ya se restauró en la corrida anterior).
        if result.rowcount:
            unvoided += 1
            products_to_restore.add(it["product_id"])
        else:
            skipped_unvoid += 1

    # 3. Restaurar stock ABSOLUTO por producto (una vez por producto, por el clamp).
    for pid in products_to_restore:
        if pid not in stock_before_by_product:
            continue
        before = stock_before_by_product[pid]
        await session.execute(
            text(
                "UPDATE products SET stock_units = :sq "
                "WHERE id = CAST(:pid AS uuid) AND tenant_id = CAST(:tid AS uuid)"
            ),
            {"sq": before, "pid": pid, "tid": tid},
        )
        existing_balance = (
            await session.execute(
                text(
                    "SELECT id FROM inventory_balances "
                    "WHERE tenant_id = CAST(:tid AS uuid) AND product_id = CAST(:pid AS uuid)"
                ),
                {"tid": tid, "pid": pid},
            )
        ).first()
        if existing_balance is not None:
            await session.execute(
                text(
                    "UPDATE inventory_balances SET current_qty = :sq "
                    "WHERE tenant_id = CAST(:tid AS uuid) AND product_id = CAST(:pid AS uuid)"
                ),
                {"sq": before, "pid": pid, "tid": tid},
            )
        else:
            await session.execute(
                text(
                    "INSERT INTO inventory_balances "
                    "(id, tenant_id, product_id, current_qty, reserved_qty, "
                    "created_at, updated_at) "
                    "VALUES (gen_random_uuid(), CAST(:tid AS uuid), CAST(:pid AS uuid), "
                    ":sq, 0, now(), now())"
                ),
                {"tid": tid, "pid": pid, "sq": before},
            )
        products_restored.append(pid)

    # 4. Auditoría de la reversa (referencia a la corrida original).
    decision_data = {
        "reverted_audit_id": run["audit_id"],
        "unvoided_movements": unvoided,
        "untagged_movements": untagged,
        "skipped_untag_noop": skipped_untag,
        "skipped_unvoid_noop": skipped_unvoid,
        "products_restored": products_restored,
    }
    await insert_decision_audit(
        session,
        tenant_id=tid,
        decision_type=_REVERT_DECISION,
        decision_data=decision_data,
        triggered_by=_TRIGGERED_BY,
    )
    return {
        "audit_id": run["audit_id"],
        "tenant_id": tid,
        "unvoided": unvoided,
        "untagged": untagged,
        "skipped_untag_noop": skipped_untag,
        "skipped_unvoid_noop": skipped_unvoid,
        "products_restored": len(products_restored),
    }


def _write_report(path: str, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    if path.lower().endswith(".csv"):
        with open(path, "w", newline="", encoding="utf-8-sig") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
    else:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(rows, fh, ensure_ascii=False, indent=2)
    print(f"\nReporte escrito en {path} ({len(rows)} fila(s)).")


async def main() -> None:
    parser = argparse.ArgumentParser(description="Revierte reconcile_untagged_adjustments.")
    parser.add_argument("--audit-id", help="UUID de la corrida (decision_audit_log) a revertir")
    parser.add_argument("--tenant", help="UUID del tenant: revierte TODAS sus corridas")
    parser.add_argument("--apply", action="store_true", help="Escribir cambios (default: dry-run)")
    parser.add_argument("--out", help="Path del reporte (CSV si .csv, si no JSON)")
    args = parser.parse_args()

    if not args.audit_id and not args.tenant:
        print("ERROR: indicá --audit-id <uuid> o --tenant <uuid>.")
        sys.exit(2)

    url, connect_args = async_engine_config()
    engine = create_async_engine(url, connect_args=connect_args)
    async with AsyncSession(engine) as session:
        if await _check_constraint_applied(session):
            print(
                f"ABORTADO: el CHECK '{_CHECK_CONSTRAINT}' YA está aplicado. Des-taggear o "
                "des-voidear un adjustment sin procedencia lo dejaría vivo + sin source_type y "
                "violaría la constraint. La reversa post-CHECK exige voidear en vez de "
                "des-taggear — decisión manual del operador. Nada se tocó."
            )
            await engine.dispose()
            sys.exit(3)

        runs = await _load_runs(session, audit_id=args.audit_id, tid=args.tenant)
        if not runs:
            print("No hay corridas UNTAGGED_ADJUSTMENT_RECONCILIATION para revertir.")
            await session.rollback()
            await engine.dispose()
            return

        mode = "APPLY" if args.apply else "DRY-RUN"
        print(f"[{mode}] reversión de {len(runs)} corrida(s)\n")

        results: list[dict[str, Any]] = []
        for run in runs:
            dd = run["decision_data"]
            n_items = len(dd.get("items") or [])
            n_stock = len(dd.get("stock_changes") or [])
            print(
                f"  audit={run['audit_id']} tenant={run['tenant_id']}: "
                f"{n_items} item(s), {n_stock} producto(s) con stock a restaurar"
            )
            if args.apply:
                results.append(await _revert_run(session, run))

        if args.apply:
            await session.commit()
            tot_unvoid = sum(r["unvoided"] for r in results)
            tot_untag = sum(r["untagged"] for r in results)
            tot_skip_unvoid = sum(r["skipped_unvoid_noop"] for r in results)
            tot_skip_untag = sum(r["skipped_untag_noop"] for r in results)
            print(
                f"\nCOMMIT: {tot_unvoid} movimiento(s) des-voideado(s), {tot_untag} "
                f"des-tagueado(s) (decision_type={_REVERT_DECISION})."
            )
            if tot_skip_unvoid or tot_skip_untag:
                print(
                    f"    (no-op, ya revertidos antes: {tot_skip_unvoid} des-voideo(s) + "
                    f"{tot_skip_untag} des-tagueo(s) omitidos)"
                )
            if args.out:
                _write_report(args.out, results)
        else:
            await session.rollback()
            print("\nDry-run: nada se escribió. Con --apply se revierten las corridas listadas.")
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
