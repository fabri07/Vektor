"""Correctivo genérico (B2): dedup de imports + reclasificación OPEX→COGS.

Reusa DataRepairService (DataRepairRun/DataRepairItem) — dry-run por defecto,
apply explícito, reversible (before_json) y auditado. Detecta, para todos los
tenants activos o uno puntual:

  1. DUPLICADOS: ventas/gastos activos idénticos (fecha, monto, desc-base,
     product_id, payment_method) CON señal de origen-import (mismo
     source_upload_id en filas distintas, o sufijo "— proveedor"/"— importado").
     Voidea todas menos la primera. Transacciones manuales idénticas legítimas
     (sin señal) NO se tocan.
  2. MISCLASIFICACIÓN OPEX→COGS: gastos OPEX cuyo texto matchea producto vendible
     del vertical (classify_expense_with_vertical) → category=INVENTORY,
     expense_type=COGS; crea/vincula producto incompleto solo con nombre+cantidad.

Usage:
    # Dry-run global (no escribe nada, lista lo que haría):
    DATABASE_URL='...' .venv/bin/python scripts/dedupe_and_reclassify.py --all-active

    # Dry-run de un tenant puntual:
    ... scripts/dedupe_and_reclassify.py --tenant <uuid>

    # Aplicar (después de validar el dry-run):
    ... scripts/dedupe_and_reclassify.py --all-active --apply

NUNCA imprime la connection URL. Correr desde backend/.
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

from app.application.services.data_repair_service import (  # noqa: E402
    detect_duplicate_imports,
    detect_misclassified_opex_to_cogs,
)


async def _preview_tenant(session: AsyncSession, tid: uuid.UUID) -> tuple[int, int]:
    """Lista (sin escribir) lo que el correctivo haría para un tenant."""
    groups = await detect_duplicate_imports(session, tenant_id=tid)
    reclass = await detect_misclassified_opex_to_cogs(session, tenant_id=tid)

    dup_total = sum(len(g.duplicates) for g in groups)
    if groups:
        print(f"  DUPLICADOS — {len(groups)} grupo(s), {dup_total} fila(s) a anular:")
        for g in groups:
            print(
                f"    [{g.entity}] mantener={g.keep_id} "
                f"anular={len(g.duplicates)} (confianza {g.confidence})"
            )
    if reclass:
        print(f"  OPEX→COGS — {len(reclass)} gasto(s) a reclasificar:")
        for c in reclass:
            print(
                f"    gasto={c.expense.id} '{c.matched_text[:40]}' "
                f"{c.expense.category} → {c.new_category} (confianza {c.confidence})"
            )
    if not groups and not reclass:
        print("  (sin candidatos)")
    return dup_total, len(reclass)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Escribir cambios (default: dry-run)")
    parser.add_argument("--tenant", help="UUID de tenant puntual")
    parser.add_argument("--all-active", action="store_true", help="Todos los tenants activos")
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
        print(f"[{mode}] dedup + reclasificación de {len(tids)} tenant(s)\n")

        total_dups = 0
        total_reclass = 0
        for tid in tids:
            print(f"tenant {tid}:")
            dups, reclass = await _preview_tenant(session, tid)
            total_dups += dups
            total_reclass += reclass
            print()

        print(f"TOTAL candidatos: {total_dups} duplicado(s) | {total_reclass} reclasificación(es)")

        if args.apply:
            # apply_repair corre ambos detectores (single-run) por tenant/global.
            from app.application.services.data_repair_service import apply_repair  # noqa: PLC0415

            scope = uuid.UUID(args.tenant) if args.tenant else None
            res = await apply_repair(session, tenant_id=scope, dry_run=False)
            await session.commit()
            print(
                f"COMMIT aplicado. run_id={res.run_id} "
                f"voided={res.sales_voided} reclasificados={res.products_updated}"
            )
        else:
            await session.rollback()
            print("Dry-run: nada se escribió.")
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
