"""Reanálisis retroactivo de archivos subidos — revisión primero, sin auto-import.

Usage:
    # Dry-run de un tenant (piloto):
    DATABASE_URL='...' R2_* env vars... \
        .venv/bin/python scripts/reanalyze_uploaded_files.py --tenant <uuid>

    # Aplicar:
    ... scripts/reanalyze_uploaded_files.py --tenant <uuid> --apply
    ... scripts/reanalyze_uploaded_files.py --all-active --apply

Qué hace (por archivo no borrado con crudo en R2):
  1. Descarga el crudo y lo re-parsea con el pipeline ACTUAL (post-fixes de
     clasificación, delimitadores, buckets y hojas preservadas).
  2. Lo ya importado NO se reinserta (cero duplicados). Los datos que el
     pipeline viejo nunca importó (buckets no confirmados, hojas no
     clasificables, filas ambiguas) van a `unclassified_records` con
     source='reanalysis' y suggested_entity del clasificador → el tenant los
     revisa y confirma en /otros. Nada se auto-importa.
  3. Dedup defensivo: filas cuyo (fecha, monto) ya existe en ventas o gastos
     del tenant se saltean.
  4. Actualiza parsed_summary_json (metadata compacta) preservando
     imported_counts, con clave `reanalysis`.

Las categorías de lo YA importado las normaliza backfill_expense_categories.py.
NUNCA imprime la connection URL. Correr desde backend/.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid
from datetime import datetime
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine  # noqa: E402

from app.application.services.file_parsing import parse_uploaded_content  # noqa: E402
from app.application.services.ingestion_import_service import (  # noqa: E402
    _parse_amount,
    _parse_date,
    _row_val,
)
from app.integrations.s3 import S3Client  # noqa: E402

_FECHA_KEYS = {"fecha", "date", "dia", "mes", "periodo"}
_MONTO_KEYS = {"monto", "importe", "total", "precio", "valor", "gasto", "venta"}

_BUCKET_ENTITY = {
    "ventas_detectadas": "sale",
    "gastos_detectados": "expense",
    "stock_detectado": "product",
    "otros_detectados": None,
}
_BUCKET_CONFIRM_KEY = {
    "ventas_detectadas": "ventas",
    "gastos_detectados": "gastos",
    "stock_detectado": "productos",
}


def _engine_url() -> str:
    raw = os.environ.get("DATABASE_URL") or os.environ.get("DATABASE_URL_SYNC")
    if not raw:
        print("ERROR: exportá DATABASE_URL antes de correr.")
        sys.exit(2)
    url = raw.strip()
    if "+asyncpg" not in url:
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
    for param in ("sslmode=require&", "&sslmode=require", "?sslmode=require",
                  "channel_binding=require&", "&channel_binding=require"):
        url = url.replace(param, "?" if param.startswith("?") else "")
    return url


async def _existing_tx_keys(session: AsyncSession, tid: uuid.UUID) -> set[tuple[str, str]]:
    """(fecha_dia, monto) de ventas+gastos existentes para dedup barato."""
    keys: set[tuple[str, str]] = set()
    for table in ("sales_entries", "expense_entries"):
        rows = await session.execute(
            text(
                f"SELECT date(transaction_date), amount FROM {table} "  # noqa: S608
                "WHERE tenant_id = :tid AND voided_at IS NULL"
            ),
            {"tid": tid},
        )
        for d, amount in rows.all():
            keys.add((str(d), f"{float(amount):.2f}"))
    return keys


def _row_key(row: dict[str, Any]) -> tuple[str, str] | None:
    raw_date = _row_val(row, _FECHA_KEYS)
    raw_amount = _row_val(row, _MONTO_KEYS)
    parsed_date = _parse_date(raw_date) if raw_date is not None else None
    parsed_amount = _parse_amount(raw_amount)
    if parsed_date is None or parsed_amount is None:
        return None
    return (str(parsed_date.date()), f"{float(parsed_amount):.2f}")


async def _process_file(
    session: AsyncSession,
    s3: S3Client,
    tid: uuid.UUID,
    file_row: Any,
    existing_keys: set[tuple[str, str]],
    apply: bool,
) -> dict[str, int]:
    stats = {"to_otros": 0, "dedup_skipped": 0, "errors": 0}
    old_summary = file_row.parsed_summary_json or {}
    if isinstance(old_summary, str):
        try:
            old_summary = json.loads(old_summary)
        except ValueError:
            old_summary = {}

    try:
        content = await s3.download(file_row.s3_key)
        new_summary = parse_uploaded_content(
            content, file_row.content_type, file_row.original_filename
        )
    except Exception as exc:  # noqa: BLE001
        print(f"      ⚠ no se pudo re-parsear: {exc}")
        stats["errors"] += 1
        return stats

    confirmed = old_summary.get("confirmed_fields") or {}
    imported = old_summary.get("imported_counts") or {}
    is_done = file_row.processing_status == "DONE"

    candidates: list[tuple[dict[str, Any], str | None, str]] = []
    for bucket, entity in _BUCKET_ENTITY.items():
        rows = new_summary.get(bucket) or []
        if not rows:
            continue
        if is_done and entity is not None:
            confirm_key = _BUCKET_CONFIRM_KEY[bucket]
            # Bucket ya importado en su momento → no recandidatear (cero duplicados).
            if confirmed.get(confirm_key) or imported.get(confirm_key, 0) > 0:
                continue
        elif not is_done and entity is not None:
            # Archivos sin confirmar siguen en el flujo normal de confirmación;
            # solo lo no clasificable (otros) va a la bandeja.
            continue
        label = "Reanálisis: " + (
            f"{bucket.replace('_detectadas', '').replace('_detectado', '')}"
        )
        for row in rows:
            candidates.append((row, entity, label))

    headers = list(candidates[0][0].keys()) if candidates else None
    for row, entity, label in candidates:
        key = _row_key(row)
        if key is not None and key in existing_keys:
            stats["dedup_skipped"] += 1
            continue
        stats["to_otros"] += 1
        if apply:
            await session.execute(
                text(
                    "INSERT INTO unclassified_records "
                    "(id, tenant_id, uploaded_file_id, source, context_label, headers, "
                    " row_data, suggested_entity, status, created_at, updated_at) "
                    "VALUES (:id, :tid, :fid, 'reanalysis', :label, "
                    " CAST(:headers AS JSONB), CAST(:row AS JSONB), :entity, 'PENDING', "
                    " now(), now())"
                ),
                {
                    "id": uuid.uuid4(),
                    "tid": tid,
                    "fid": file_row.id,
                    "label": label[:200],
                    "headers": json.dumps([h for h in (headers or []) if h != "__context__"]),
                    "row": json.dumps(
                        {
                            k: ("" if v is None else str(v))
                            for k, v in row.items()
                            if k != "__context__"
                        },
                        ensure_ascii=False,
                    ),
                    "entity": entity,
                },
            )

    if apply and (stats["to_otros"] or stats["dedup_skipped"]):
        compact = {
            k: v
            for k, v in new_summary.items()
            if k
            not in (
                "ventas_detectadas",
                "gastos_detectados",
                "stock_detectado",
                "otros_detectados",
                "preview_rows",
                "mapping_contexts",
            )
        }
        compact["confirmed_fields"] = confirmed
        compact["imported_counts"] = imported
        compact["reanalysis"] = {
            "at": datetime.now().isoformat(timespec="seconds"),
            "rows_to_otros": stats["to_otros"],
            "dedup_skipped": stats["dedup_skipped"],
        }
        await session.execute(
            text(
                "UPDATE uploaded_files SET parsed_summary_json = CAST(:summary AS JSONB) "
                "WHERE id = :id"
            ),
            {"summary": json.dumps(compact, ensure_ascii=False, default=str), "id": file_row.id},
        )
    return stats


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Escribir cambios (default: dry-run)")
    parser.add_argument("--tenant", help="UUID de tenant puntual (piloto)")
    parser.add_argument("--all-active", action="store_true", help="Todos los tenants activos")
    args = parser.parse_args()

    if not args.tenant and not args.all_active:
        print("ERROR: indicá --tenant <uuid> (piloto) o --all-active.")
        sys.exit(2)

    engine = create_async_engine(_engine_url(), connect_args={"ssl": "require"})
    s3 = S3Client()
    async with AsyncSession(engine) as session:
        if args.tenant:
            tids = [uuid.UUID(args.tenant)]
        else:
            rows = await session.execute(
                text("SELECT tenant_id FROM tenants WHERE status = 'ACTIVE'")
            )
            tids = [r[0] for r in rows.all()]

        mode = "APPLY" if args.apply else "DRY-RUN"
        print(f"[{mode}] reanálisis de {len(tids)} tenant(s)")
        totals = {"files": 0, "to_otros": 0, "dedup_skipped": 0, "errors": 0}

        for tid in tids:
            files = (
                await session.execute(
                    text(
                        "SELECT id, original_filename, content_type, s3_key, "
                        "processing_status, parsed_summary_json "
                        "FROM uploaded_files WHERE tenant_id = :tid "
                        "AND deleted_at IS NULL AND s3_key IS NOT NULL "
                        "ORDER BY created_at"
                    ),
                    {"tid": tid},
                )
            ).all()
            if not files:
                continue
            print(f"\n  tenant {tid}: {len(files)} archivo(s)")
            existing_keys = await _existing_tx_keys(session, tid)
            for f in files:
                print(f"    ── {f.original_filename!r} [{f.processing_status}]")
                stats = await _process_file(session, s3, tid, f, existing_keys, args.apply)
                print(
                    f"       a_otros={stats['to_otros']} "
                    f"dedup={stats['dedup_skipped']} errores={stats['errors']}"
                )
                totals["files"] += 1
                for k in ("to_otros", "dedup_skipped", "errors"):
                    totals[k] += stats[k]

        print(
            f"\nTOTAL: {totals['files']} archivos | {totals['to_otros']} filas a Otros | "
            f"{totals['dedup_skipped']} dedup | {totals['errors']} errores"
        )
        if args.apply:
            await session.commit()
            print("COMMIT aplicado.")
        else:
            await session.rollback()
            print("Dry-run: nada se escribió.")
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
