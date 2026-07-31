"""Limpia los datos que quedaron vivos de archivos ya BORRADOS.

Hasta el commit del borrado-que-revierte, `DELETE /ingestion/files/{id}` solo
hacía `deleted_at = now()`: el archivo desaparecía de la lista y sus ventas,
gastos, movimientos de stock y filas en "Otros" seguían contando en el
dashboard. Este script barre esa deuda histórica.

Es el mismo criterio que aplica el endpoint nuevo, corrido hacia atrás sobre los
archivos que ya se borraron. Sistémico (todos los tenants), dry-run por defecto,
auditado y reversible.

QUÉ TOCA
    ventas / gastos   → soft delete (`voided_at` + `void_reason='USER_CANCELLED'`),
                        filtrados por `source_upload_id` del archivo borrado.
    movimientos stock → `void_movement`: revierte el efecto de cada movimiento
                        sobre `stock_units`/`inventory_balances`. INCREMENTAL,
                        nunca recomputado desde el ledger.
    "Otros"           → borra las filas (`unclassified_records.uploaded_file_id`);
                        nunca fueron dato de negocio, son staging del archivo.

QUÉ **NO** TOCA
    productos → los archivos borrados en su momento se importaron sin el ledger
                de reversa (`ingestion_version < 3`), así que NO se puede saber
                cuáles CREÓ ese archivo y cuáles ya existían. Desactivar un
                producto preexistente sería peor que dejarlo, así que se cuentan
                y se reportan como AMBIGUOS, sin tocarlos.

COBERTURA, no solo hallazgos: el reporte dice cuántos archivos borrados se
revisaron, cuántos tenían datos vivos y cuántos productos quedaron ambiguos. Un
"0 filas" sin ese denominador es indistinguible de "el script no encontró nada".

Usage:
    # Dry-run global (default): no escribe nada.
    DATABASE_URL='postgresql://...' .venv/bin/python \
        scripts/purge_deleted_file_data.py --all-active --out plan.csv

    # Un tenant puntual:
    ... scripts/purge_deleted_file_data.py --tenant <uuid>

    # Aplicar, después de revisar el CSV:
    ... scripts/purge_deleted_file_data.py --tenant <uuid> --apply

CÓMO REVERTIR
    Cada corrida con --apply deja un `decision_audit_log` con
    decision_type='DELETED_FILE_DATA_PURGE' y, por archivo, los ids afectados.
    Para revertir un archivo:
        UPDATE sales_entries SET voided_at=NULL, void_reason=NULL
          WHERE id = ANY(<sale_ids del audit>);
        UPDATE expense_entries SET voided_at=NULL, void_reason=NULL
          WHERE id = ANY(<expense_ids del audit>);
    Los movimientos y su efecto en stock se revierten des-voideando el movimiento
    y sumando de nuevo su `qty` (con signo) al producto. Las filas de "Otros"
    borradas NO son recuperables: revisá el dry-run antes de aplicar.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import sys
from datetime import UTC, datetime
from typing import Any

import asyncpg

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _db import normalize_dsn  # noqa: E402

# Espejo del valor que usa el endpoint (set cerrado de ck_sales_entries_void_reason).
VOID_REASON = "USER_CANCELLED"


def p(titulo: str) -> None:
    print(f"\n{'=' * 74}\n  {titulo}\n{'=' * 74}")


async def _tenants(conn: asyncpg.Connection, tenant: str | None, todos: bool) -> list[str]:
    if tenant:
        return [tenant]
    if not todos:
        raise SystemExit("Pasá --tenant <uuid> o --all-active.")
    filas = await conn.fetch("SELECT tenant_id::text FROM tenants WHERE status = 'ACTIVE'")
    return [f["tenant_id"] for f in filas]


async def _archivos_borrados(conn: asyncpg.Connection, tenant_id: str) -> list[asyncpg.Record]:
    return await conn.fetch(
        "SELECT id::text AS id, original_filename, deleted_at, ingestion_version "
        "FROM uploaded_files "
        "WHERE tenant_id = $1 AND deleted_at IS NOT NULL "
        "ORDER BY deleted_at",
        tenant_id,
    )


async def _relevar(
    conn: asyncpg.Connection, tenant_id: str, file_id: str
) -> dict[str, Any]:
    """Qué quedó vivo de este archivo borrado. Solo SELECT."""
    ventas = await conn.fetch(
        "SELECT id::text AS id FROM sales_entries "
        "WHERE tenant_id=$1 AND source_upload_id=$2 AND voided_at IS NULL",
        tenant_id,
        file_id,
    )
    gastos = await conn.fetch(
        "SELECT id::text AS id FROM expense_entries "
        "WHERE tenant_id=$1 AND source_upload_id=$2 AND voided_at IS NULL",
        tenant_id,
        file_id,
    )
    movimientos = await conn.fetch(
        "SELECT id::text AS id, product_id::text AS product_id, qty "
        "FROM inventory_movements "
        "WHERE tenant_id=$1 AND source_upload_id=$2 AND voided_at IS NULL",
        tenant_id,
        file_id,
    )
    otros = await conn.fetchval(
        "SELECT count(*) FROM unclassified_records "
        "WHERE tenant_id=$1 AND uploaded_file_id=$2",
        tenant_id,
        file_id,
    )
    # Productos que este archivo pudo haber creado. Sin ledger no se sabe cuáles;
    # se cuentan los que tienen un movimiento del archivo, solo para informar.
    productos_ambiguos = await conn.fetchval(
        "SELECT count(DISTINCT product_id) FROM inventory_movements "
        "WHERE tenant_id=$1 AND source_upload_id=$2 AND product_id IS NOT NULL",
        tenant_id,
        file_id,
    )
    return {
        "sale_ids": [r["id"] for r in ventas],
        "expense_ids": [r["id"] for r in gastos],
        "movimientos": [dict(r) for r in movimientos],
        "otros": otros or 0,
        "productos_ambiguos": productos_ambiguos or 0,
    }


async def _aplicar(
    conn: asyncpg.Connection, tenant_id: str, file_id: str, hallazgo: dict[str, Any]
) -> None:
    """Aplica la reversa de UN archivo, en una transacción."""
    ahora = datetime.now(UTC)
    async with conn.transaction():
        if hallazgo["sale_ids"]:
            await conn.execute(
                "UPDATE sales_entries SET voided_at=$1, void_reason=$2 "
                "WHERE id = ANY($3::uuid[])",
                ahora,
                VOID_REASON,
                hallazgo["sale_ids"],
            )
        if hallazgo["expense_ids"]:
            await conn.execute(
                "UPDATE expense_entries SET voided_at=$1, void_reason=$2 "
                "WHERE id = ANY($3::uuid[])",
                ahora,
                VOID_REASON,
                hallazgo["expense_ids"],
            )

        # Movimientos: voidear + revertir su efecto en stock. INCREMENTAL —
        # restar `qty` (que viene con signo) deshace exactamente lo que aportó.
        for mov in hallazgo["movimientos"]:
            await conn.execute(
                "UPDATE inventory_movements SET voided_at=$1 WHERE id=$2::uuid",
                ahora,
                mov["id"],
            )
            if mov["product_id"] is not None:
                await conn.execute(
                    "UPDATE products SET stock_units = stock_units - $1 WHERE id=$2::uuid",
                    mov["qty"],
                    mov["product_id"],
                )
                await conn.execute(
                    "UPDATE inventory_balances SET current_qty = current_qty - $1 "
                    "WHERE tenant_id=$2 AND product_id=$3::uuid",
                    mov["qty"],
                    tenant_id,
                    mov["product_id"],
                )

        if hallazgo["otros"]:
            await conn.execute(
                "DELETE FROM unclassified_records "
                "WHERE tenant_id=$1 AND uploaded_file_id=$2",
                tenant_id,
                file_id,
            )

        await conn.execute(
            "INSERT INTO decision_audit_log "
            "(id, tenant_id, decision_type, decision_data, triggered_by, context, created_at) "
            "VALUES (gen_random_uuid(), $1, $2, $3, $4, $5, $6)",
            tenant_id,
            "DELETED_FILE_DATA_PURGE",
            json.dumps(
                {
                    "file_id": file_id,
                    "sale_ids": hallazgo["sale_ids"],
                    "expense_ids": hallazgo["expense_ids"],
                    "movimientos": hallazgo["movimientos"],
                    "otros_borrados": hallazgo["otros"],
                    "productos_ambiguos_no_tocados": hallazgo["productos_ambiguos"],
                }
            ),
            "scripts/purge_deleted_file_data.py",
            json.dumps({"source": "purge_deleted_file_data"}),
            ahora,
        )


def _escribir_reporte(path: str, filas: list[dict[str, Any]]) -> None:
    if not filas:
        print("  (sin filas que reportar)")
        return
    if path.endswith(".csv"):
        with open(path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(filas[0].keys()))
            writer.writeheader()
            writer.writerows(filas)
    else:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(filas, fh, indent=2, ensure_ascii=False, default=str)
    print(f"  reporte escrito en {path}")


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant", help="UUID de tenant puntual")
    parser.add_argument("--all-active", action="store_true", help="Todos los tenants activos")
    parser.add_argument("--apply", action="store_true", help="Escribir (default: dry-run)")
    parser.add_argument("--out", help="Path del reporte (CSV si .csv, si no JSON)")
    args = parser.parse_args()

    raw = os.environ.get("DATABASE_URL") or os.environ.get("DATABASE_URL_SYNC")
    if not raw:
        print("ERROR: exportá DATABASE_URL con la URL de la base.")
        sys.exit(2)
    dsn, sslarg = normalize_dsn(raw)
    conn = await asyncpg.connect(dsn, ssl=sslarg)

    modo = "APPLY" if args.apply else "DRY-RUN"
    p(f"PURGA DE DATOS DE ARCHIVOS BORRADOS — {modo}")

    filas_reporte: list[dict[str, Any]] = []
    total_archivos = 0
    con_datos = 0
    tot_ventas = tot_gastos = tot_movs = tot_otros = tot_ambiguos = 0

    try:
        for tenant_id in await _tenants(conn, args.tenant, args.all_active):
            for archivo in await _archivos_borrados(conn, tenant_id):
                total_archivos += 1
                hallazgo = await _relevar(conn, tenant_id, archivo["id"])
                n_ventas = len(hallazgo["sale_ids"])
                n_gastos = len(hallazgo["expense_ids"])
                n_movs = len(hallazgo["movimientos"])
                if not (n_ventas or n_gastos or n_movs or hallazgo["otros"]):
                    continue

                con_datos += 1
                tot_ventas += n_ventas
                tot_gastos += n_gastos
                tot_movs += n_movs
                tot_otros += hallazgo["otros"]
                tot_ambiguos += hallazgo["productos_ambiguos"]

                filas_reporte.append(
                    {
                        "tenant_id": tenant_id,
                        "file_id": archivo["id"],
                        "archivo": archivo["original_filename"],
                        "borrado_el": archivo["deleted_at"],
                        "ingestion_version": archivo["ingestion_version"],
                        "ventas": n_ventas,
                        "gastos": n_gastos,
                        "movimientos_stock": n_movs,
                        "otros": hallazgo["otros"],
                        "productos_ambiguos_no_tocados": hallazgo["productos_ambiguos"],
                    }
                )
                print(
                    f"  [{tenant_id[:8]}] {archivo['original_filename'][:40]:40} "
                    f"ventas={n_ventas} gastos={n_gastos} movs={n_movs} "
                    f"otros={hallazgo['otros']} ambiguos={hallazgo['productos_ambiguos']}"
                )

                if args.apply:
                    await _aplicar(conn, tenant_id, archivo["id"], hallazgo)

        # COBERTURA: el denominador, no solo los hallazgos.
        p("COBERTURA")
        print(f"  archivos borrados revisados : {total_archivos}")
        print(f"  con datos vivos             : {con_datos}")
        print(f"  ventas a anular             : {tot_ventas}")
        print(f"  gastos a anular             : {tot_gastos}")
        print(f"  movimientos a revertir      : {tot_movs}")
        print(f"  filas de 'Otros' a borrar   : {tot_otros}")
        print(f"  productos AMBIGUOS (no se tocan): {tot_ambiguos}")
        if tot_ambiguos:
            print(
                "\n  Esos productos vinieron de archivos importados sin ledger de\n"
                "  reversa: no se puede saber cuáles creó el archivo y cuáles ya\n"
                "  existían. Quedan vivos — revisalos a mano en Productos."
            )
        if not args.apply and con_datos:
            print("\n  DRY-RUN: no se escribió nada. Revisá el reporte y volvé con --apply.")

        if args.out:
            _escribir_reporte(args.out, filas_reporte)
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
