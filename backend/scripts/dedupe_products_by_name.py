"""Dedup de productos — LECTURA/PLANIFICACIÓN (F3-T4), dry-run.

Detecta grupos de productos duplicados de un tenant (aristas F2 fuerte/medio/débil),
elige el canónico, clasifica conflictos de identidad y la decisión de stock por
procedencia, calcula un fingerprint determinístico por grupo y persiste el PLAN
(``DataRepairRun`` + ``DataRepairItem``) SIN tocar ningún dato de negocio.

Toda la lógica vive en ``app/application/services/product_dedup_service.py`` (testeable
sin CLI). Este script solo orquesta: precondición, plan, persistencia, CSV y auditoría.

La EJECUCIÓN real de las mutaciones (fusionar, re-apuntar FKs, consolidar/borrar
balances, desactivar duplicados) es **F3-T5** — ``--apply``. La REVERSA
(``--revert-run <run_uuid>``) es **F3-T6**: revierte un run de APPLY con la inversa
exacta e incremental de cada mutación, validando el estado ACTUAL contra el
``after_json`` que el apply dejó. Mutuamente excluyente con ``--apply``. NO existe
``--force`` en v1 (forzar la reversa de inventario tras actividad real corrompe stock).

## Lease (advisory exclusive)

En el dry-run (T4) NO se toma el advisory lock EXCLUSIVE de reparación: el plan solo
lee negocio y escribe audit/plan. El ``--apply`` (T5) sí:

- Toma el **lease observable** (``maintenance_lock_service.acquire``) al inicio y lo
  ``release`` en el ``finally`` (si el proceso muere, el lease expira solo por TTL).
- Lo ``renew`` (heartbeat) antes de cada grupo, para runs largos.
- Toma la **barrera transaccional real** (``acquire_maintenance_lock_exclusive``)
  DENTRO de la transacción de cada grupo (se libera al commit/rollback del grupo).

Cada grupo se aplica en su PROPIA transacción (no una gigante): un grupo que falla o
cuyo fingerprint cambió NO tumba a los demás → el run termina ``PARTIALLY_APPLIED``.

Usage:
    # Dry-run (default) de un tenant: detecta, planifica y persiste el plan.
    DATABASE_URL='postgresql://...' .venv/bin/python scripts/dedupe_products_by_name.py \
        --tenant <uuid> --out plan.csv

    # Dry-run global (todos los tenants activos):
    ... scripts/dedupe_products_by_name.py --all-active --out plan.csv

    # Apply (ejecución): consume un run dry-run previo (revalida el fingerprint por grupo).
    ... scripts/dedupe_products_by_name.py --tenant <uuid> --apply --source-run-id <run_uuid>

    # Revert (reversa): deshace un run de APPLY (valida el estado actual vs after_json).
    ... scripts/dedupe_products_by_name.py --tenant <uuid> --revert-run <apply_run_uuid>

    # Escape hatch (NO es un run de dedup): completar identidad NULL (backfill mínimo).
    ... scripts/dedupe_products_by_name.py --tenant <uuid> --repair-missing-identity

NUNCA imprime la connection URL. Correr desde backend/.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _db import async_engine_config  # noqa: E402
from sqlalchemy import select, text, update  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine  # noqa: E402

from app.application.services import maintenance_lock_service as mls  # noqa: E402
from app.application.services import product_dedup_service as dedup  # noqa: E402
from app.domain.text_norm import (  # noqa: E402
    normalize_barcode,
    normalize_brand,
    normalize_product_name,
    normalize_sku,
)
from app.persistence.models.product import Product  # noqa: E402

_REPAIR_MISSING_BATCH = 500
# TTL del lease de mantenimiento (se renueva —heartbeat— antes de cada grupo).
_LEASE_TTL_SECONDS = 300


async def _repair_missing_identity(session: AsyncSession, tid: uuid.UUID) -> int:
    """Escape hatch (NO un run de dedup): setea las columnas ``*_normalized`` NULL de
    productos ACTIVOS con los helpers de ``text_norm``, en batches. Mínimo a propósito
    — el backfill canónico es la migración ``20260731_0002``."""
    total = 0
    while True:
        rows = (
            await session.execute(
                select(
                    Product.id, Product.name, Product.sku, Product.barcode, Product.custom_fields
                )
                .where(
                    Product.tenant_id == tid,
                    Product.is_active.is_(True),
                    Product.name_normalized.is_(None),
                )
                .limit(_REPAIR_MISSING_BATCH)
            )
        ).all()
        if not rows:
            break
        for pid, name, sku, barcode, custom in rows:
            marca = custom.get("marca") if isinstance(custom, dict) else None
            await session.execute(
                update(Product)
                .where(Product.id == pid)
                .values(
                    name_normalized=normalize_product_name(name) or None,
                    sku_normalized=normalize_sku(sku),
                    barcode_normalized=normalize_barcode(barcode),
                    brand_normalized=normalize_brand(marca),
                )
            )
            total += 1
        await session.flush()
    return total


def _write_csv(path: str, plans: list[tuple[uuid.UUID, dedup.DedupPlan]]) -> None:
    """CSV legible por grupo (todos los tenants corridos): canónico, duplicados,
    aristas, decisión de identidad y de stock, fingerprint. Reporta cobertura (no
    silencia el desglose)."""
    fieldnames = [
        "tenant_id",
        "group_id",
        "kind",
        "requires_review",
        "canonical_id",
        "member_ids",
        "edges",
        "review_reasons",
        "stock_decision",
        "fingerprint",
    ]
    total = 0
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for tid, plan in plans:
            for g in plan.groups:
                writer.writerow(
                    {
                        "tenant_id": str(tid),
                        "group_id": g.group_id,
                        "kind": g.kind,
                        "requires_review": g.requires_review,
                        "canonical_id": str(g.canonical_id) if g.canonical_id else "",
                        "member_ids": ";".join(str(m) for m in g.member_ids),
                        "edges": ";".join(f"{e.kind}:{e.reason}" for e in g.edges),
                        "review_reasons": ";".join(g.review_reasons),
                        "stock_decision": (
                            f"{g.stock_decision.kind}:{g.stock_decision.delta}"
                            if g.stock_decision is not None
                            else ""
                        ),
                        "fingerprint": g.fingerprint or "",
                    }
                )
                total += 1
    print(f"\nCSV escrito en {path} ({total} grupo(s)).")


def _print_coverage(tid: uuid.UUID, plan: dedup.DedupPlan) -> None:
    cov = plan.coverage()
    print(
        f"tenant {tid}: {cov['groups_detected']} grupo(s) — "
        f"mergeables={cov['mergeable_groups']} review={cov['review_groups']} "
        f"(por motivo: {cov['review_by_reason']})"
    )


async def _run_tenant(session: AsyncSession, tid: uuid.UUID) -> dedup.DedupPlan | None:
    """Corre el plan dry-run de un tenant. Aborta si falta el backfill de identidad."""
    missing = await dedup.count_active_products_missing_identity(session, tid)
    if missing > 0:
        print(
            f"  ⚠ tenant {tid}: {missing} producto(s) ACTIVO(s) con name_normalized NULL. "
            f"Correr la migración de backfill 20260731_0002 (o "
            f"--repair-missing-identity) ANTES de dedup. Run SALTEADO."
        )
        return None
    plan = await dedup.plan_dedup(session, tid)
    await dedup.persist_dedup_plan(session, tid, plan)
    _print_coverage(tid, plan)
    return plan


async def _apply_tenant(
    session: AsyncSession, tid: uuid.UUID, source_run_id: uuid.UUID
) -> dedup.ApplyResult | None:
    """Apply de un tenant: toma el lease observable, ejecuta ``apply_dedup_plan``
    (barrera exclusive + fingerprint por grupo) y libera el lease en el ``finally``."""
    lease_id = await mls.acquire(
        session,
        tid,
        reason="product_dedup",
        ttl_seconds=_LEASE_TTL_SECONDS,
        created_by="script:dedupe_products_by_name",
    )
    if lease_id is None:
        print(f"  ⚠ tenant {tid}: ya hay un lease de mantenimiento activo. Apply SALTEADO.")
        return None
    await session.commit()
    try:
        result = await dedup.apply_dedup_plan(
            session,
            tid,
            source_run_id,
            lease_id=lease_id,
            ttl_seconds=_LEASE_TTL_SECONDS,
        )
        print(
            f"tenant {tid}: run {result.run_id} → {result.status} "
            f"(applied={result.groups_applied} skipped={result.groups_skipped} "
            f"failed={result.groups_failed} de {result.groups_total})"
        )
        for gr in result.group_results:
            if gr["status"] != "applied":
                print(f"    grupo {gr['group_id']}: {gr['status']} ({gr['reason']})")
        return result
    finally:
        await mls.release(session, lease_id)
        await session.commit()


async def _revert_tenant(
    session: AsyncSession, tid: uuid.UUID, run_id: uuid.UUID
) -> dedup.RevertResult | None:
    """Reversa de un run de APPLY: toma el lease observable, ejecuta
    ``revert_dedup_run`` (barrera exclusive + validación contra after_json por grupo) y
    libera el lease en el ``finally``. NO existe ``--force`` (v1)."""
    lease_id = await mls.acquire(
        session,
        tid,
        reason="product_dedup_revert",
        ttl_seconds=_LEASE_TTL_SECONDS,
        created_by="script:dedupe_products_by_name",
    )
    if lease_id is None:
        print(f"  ⚠ tenant {tid}: ya hay un lease de mantenimiento activo. Revert SALTEADO.")
        return None
    await session.commit()
    try:
        result = await dedup.revert_dedup_run(
            session, tid, run_id, lease_id=lease_id, ttl_seconds=_LEASE_TTL_SECONDS
        )
        print(
            f"tenant {tid}: run {result.run_id} → {result.status} "
            f"(reverted={result.groups_reverted} skipped={result.groups_skipped} "
            f"failed={result.groups_failed} de {result.groups_total})"
        )
        for gr in result.group_results:
            if gr["status"] != "reverted":
                print(f"    grupo {gr['group_id']}: {gr['status']} ({gr['reason']})")
        return result
    finally:
        await mls.release(session, lease_id)
        await session.commit()


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant", help="UUID de tenant puntual")
    parser.add_argument("--all-active", action="store_true", help="Todos los tenants activos")
    parser.add_argument("--out", help="Path del CSV del plan (un archivo por corrida)")
    parser.add_argument(
        "--repair-missing-identity",
        action="store_true",
        help="Escape hatch: completar *_normalized NULL (NO es un run de dedup)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Ejecuta las mutaciones planificadas por un run dry-run (exige --source-run-id)",
    )
    parser.add_argument(
        "--source-run-id",
        help="UUID del run dry-run (T4) a aplicar (obligatorio con --apply)",
    )
    parser.add_argument(
        "--revert-run",
        help="UUID de un run de APPLY (T5) a revertir (inversa exacta validada contra "
        "after_json). Mutuamente excluyente con --apply. NO existe --force en v1.",
    )
    args = parser.parse_args()

    # ── Camino REVERT (F3-T6) ─────────────────────────────────────────────────────
    if args.revert_run:
        if args.apply:
            print("ERROR: --revert-run es mutuamente excluyente con --apply.")
            sys.exit(2)
        if not args.tenant:
            print("ERROR: --revert-run exige --tenant <uuid> (la reversa es por tenant).")
            sys.exit(2)
        tid = uuid.UUID(args.tenant)
        run_id = uuid.UUID(args.revert_run)
        url, connect_args = async_engine_config()
        engine = create_async_engine(url, connect_args=connect_args)
        try:
            async with AsyncSession(engine, expire_on_commit=False) as session:
                print(f"[REVERT] revirtiendo run de apply {run_id} del tenant {tid}\n")
                try:
                    await _revert_tenant(session, tid, run_id)
                except ValueError as exc:
                    print(f"ERROR: {exc}")
                    sys.exit(2)
        finally:
            await engine.dispose()
        return

    if not args.tenant and not args.all_active:
        print("ERROR: indicá --tenant <uuid> o --all-active.")
        sys.exit(2)

    # ── Camino APPLY (F3-T5) ──────────────────────────────────────────────────────
    if args.apply:
        if not args.source_run_id:
            print("ERROR: --apply exige --source-run-id <run_uuid> (el run dry-run de T4).")
            sys.exit(2)
        if not args.tenant:
            print("ERROR: --apply exige --tenant <uuid> (el apply es por tenant).")
            sys.exit(2)
        tid = uuid.UUID(args.tenant)
        source_run_id = uuid.UUID(args.source_run_id)
        url, connect_args = async_engine_config()
        engine = create_async_engine(url, connect_args=connect_args)
        try:
            async with AsyncSession(engine, expire_on_commit=False) as session:
                print(f"[APPLY] ejecutando plan {source_run_id} del tenant {tid}\n")
                try:
                    await _apply_tenant(session, tid, source_run_id)
                except ValueError as exc:
                    print(f"ERROR: {exc}")
                    sys.exit(2)
        finally:
            await engine.dispose()
        return

    url, connect_args = async_engine_config()
    engine = create_async_engine(url, connect_args=connect_args)
    try:
        async with AsyncSession(engine) as session:
            if args.tenant:
                tids = [uuid.UUID(args.tenant)]
            else:
                rows = await session.execute(
                    text("SELECT tenant_id FROM tenants WHERE status = 'ACTIVE'")
                )
                tids = [r[0] for r in rows.all()]

            if args.repair_missing_identity:
                total = 0
                for tid in tids:
                    n = await _repair_missing_identity(session, tid)
                    total += n
                    print(f"tenant {tid}: {n} producto(s) con identidad completada.")
                await session.commit()
                print(f"COMMIT: {total} producto(s) con *_normalized backfilleado(s).")
                return

            print(f"[DRY-RUN] plan de dedup de productos en {len(tids)} tenant(s)\n")
            plans: list[tuple[uuid.UUID, dedup.DedupPlan]] = []
            for tid in tids:
                plan = await _run_tenant(session, tid)
                if plan is not None:
                    plans.append((tid, plan))
                print()

            # El PLAN (run + items + auditoría) SÍ se persiste — es el deliverable de T4.
            await session.commit()

            if args.out and plans:
                _write_csv(args.out, plans)
            print("Plan persistido (dry_run=True). Ningún dato de negocio fue modificado.")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
