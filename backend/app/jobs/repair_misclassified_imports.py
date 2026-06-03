"""Job CLI para reparar importaciones mal clasificadas.

Uso:
    python -m app.jobs.repair_misclassified_imports --dry-run
    python -m app.jobs.repair_misclassified_imports --apply
    python -m app.jobs.repair_misclassified_imports --tenant-id <uuid>
    python -m app.jobs.repair_misclassified_imports --apply --source-run-id <uuid>

Default: --dry-run. Nunca modifica datos sin --apply explícito.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid

from app.observability.logger import get_logger

logger = get_logger(__name__)


async def _run(
    dry_run: bool,
    tenant_id: uuid.UUID | None,
    source_run_id: uuid.UUID | None,
) -> int:
    from app.application.services.data_repair_service import apply_repair  # noqa: PLC0415
    from app.persistence.db.session import async_session_factory  # noqa: PLC0415

    async with async_session_factory() as db:
        result = await apply_repair(
            db,
            tenant_id=tenant_id,
            dry_run=dry_run,
            source_run_id=source_run_id,
        )
        await db.commit()

    mode = "DRY-RUN" if dry_run else "APPLY"
    logger.info(
        f"repair.{mode.lower()}.summary",
        run_id=str(result.run_id),
        candidates_found=result.candidates_found,
        sales_detected=result.sales_detected,
        sales_voided=result.sales_voided,
        products_detected=result.products_detected,
        products_created=result.products_created,
        products_updated=result.products_updated,
    )

    print(f"\n{'='*60}")
    print(f"  Repair {mode} — run_id: {result.run_id}")
    print(f"{'='*60}")
    print(f"  Candidatos encontrados:   {result.candidates_found}")
    print(f"  Ventas detectadas:        {result.sales_detected}")
    print(f"  Ventas anuladas:          {result.sales_voided}")
    print(f"  Productos detectados:     {result.products_detected}")
    print(f"  Productos creados:        {result.products_created}")
    print(f"  Productos actualizados:   {result.products_updated}")
    if result.tenant_results:
        print("\n  Detalle por tenant:")
        for tr in result.tenant_results:
            status = tr.get("status", "?")
            tid = tr.get("tenant_id", "?")[:8]
            error = tr.get("error", "")
            if error:
                print(f"    [{status}] tenant={tid}... error={error}")
            else:
                print(
                    f"    [{status}] tenant={tid}... sales={tr.get('sales_processed',0)} "
                    f"products={tr.get('product_rows',0)}"
                )
    if dry_run:
        print("\n  ⚠️  Modo dry-run: no se modificaron datos.")
        print(f"  Para aplicar: --apply --source-run-id {result.run_id}")
    print(f"{'='*60}\n")

    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Repara importaciones de productos mal clasificadas como ventas."
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--dry-run", action="store_true", default=True, help="Solo detectar, no modificar (default)"
    )
    group.add_argument("--apply", action="store_true", default=False, help="Aplicar la reparación")
    parser.add_argument(
        "--tenant-id", type=str, default=None, help="Limitar a un tenant específico"
    )
    parser.add_argument("--source-run-id", type=str, default=None, help="UUID de un dry-run previo")

    args = parser.parse_args()

    dry_run = not args.apply

    tenant_id: uuid.UUID | None = None
    if args.tenant_id:
        try:
            tenant_id = uuid.UUID(args.tenant_id)
        except ValueError:
            print(f"ERROR: tenant-id inválido: {args.tenant_id}", file=sys.stderr)
            sys.exit(1)

    source_run_id: uuid.UUID | None = None
    if args.source_run_id:
        try:
            source_run_id = uuid.UUID(args.source_run_id)
        except ValueError:
            print(f"ERROR: source-run-id inválido: {args.source_run_id}", file=sys.stderr)
            sys.exit(1)

    exit_code = asyncio.run(_run(dry_run=dry_run, tenant_id=tenant_id, source_run_id=source_run_id))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
