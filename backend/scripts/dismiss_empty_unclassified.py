"""F-O.4: borra físicamente las filas 100% vacías, históricas, de la bandeja
"Otros" de un tenant — el fix de captura (F-O.4a, `_capture_unclassified`)
solo previene las NUEVAS; esto limpia lo que ya está persistido.

Usage:
    # Dry-run (default): lista qué borraría + exporta un CSV de los IDs.
    DATABASE_URL='postgresql://...' .venv/bin/python scripts/dismiss_empty_unclassified.py \
        --tenant <uuid>

    # Aplicar (requiere --confirm además de --apply):
    ... --tenant <uuid> --apply --confirm

Criterio de "vacía": exactamente el mismo que ``_fila_con_contenido`` usa en
producción para NO capturar — ``row_data`` sin ningún valor real (None, "",
espacios, o el string "nan"). Nunca borra por coincidencia de nombre de
columna ni de motivo (`context_label`) — solo por CONTENIDO.

Guard obligatorio: una fila con ``ROW_REF_KEY`` (``__row_ref__``, F-O.2 — el
vínculo con una clasificación humana desde /otros) NUNCA se borra, aunque su
``row_data`` visible sea vacío. "Vacía" es sobre el dato de negocio, no sobre
la metadata interna de vínculo.

Solo alcanza filas ``PENDING`` — las ya ``IMPORTED``/``DISMISSED`` son
historial resuelto, no basura de captura.

DELETE físico (decisión de producto, distinto del descarte en bloque de
F-O.3 que es soft ``DISMISSED`` — acá son filas sin ningún dato de negocio,
no una decisión sobre datos reales) dentro de una única transacción, con
auditoría en ``decision_audit_log``. NUNCA imprime la connection URL.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import os
import sys
import uuid
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _db import async_engine_config, insert_decision_audit  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine  # noqa: E402

from app.application.services.ingestion_import_service import (  # noqa: E402
    ROW_REF_KEY,
    _fila_con_contenido,
)

_DECISION_TYPE = "UNCLASSIFIED_EMPTY_ROWS_DELETED"


def _is_blank_and_unlinked(row_data: dict[str, object]) -> bool:
    """Vacía (sin contenido real) Y sin vínculo humano (`ROW_REF_KEY`)."""
    if ROW_REF_KEY in row_data:
        return False
    return not _fila_con_contenido(row_data)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant", required=True, help="UUID del tenant (obligatorio)")
    parser.add_argument("--apply", action="store_true", help="Escribir (default: dry-run)")
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Confirmación EXPLÍCITA adicional — --apply solo no alcanza.",
    )
    parser.add_argument(
        "--export-csv",
        default=None,
        help="Ruta del CSV con los IDs candidatos (default: auto-generado en cwd).",
    )
    args = parser.parse_args()

    if args.apply and not args.confirm:
        print(
            "ERROR: --apply requiere --confirm además — un DELETE físico no se "
            "dispara con un solo flag."
        )
        sys.exit(2)

    tid = uuid.UUID(args.tenant)
    url, connect_args = async_engine_config()
    engine = create_async_engine(url, connect_args=connect_args)
    async with AsyncSession(engine) as session:
        rows = (
            await session.execute(
                text(
                    "SELECT id, row_data, context_label, uploaded_file_id, created_at "
                    "FROM unclassified_records "
                    "WHERE tenant_id = :tid AND status = 'PENDING' "
                    "ORDER BY created_at"
                ),
                {"tid": tid},
            )
        ).all()

        candidates = [r for r in rows if _is_blank_and_unlinked(dict(r.row_data or {}))]
        # Reporte, no criterio de borrado: filas que SERÍAN vacías sin el
        # vínculo humano — para que el operador vea cuántas se preservaron
        # a propósito, no solo cuántas se van a borrar.
        skipped_linked = [
            r
            for r in rows
            if ROW_REF_KEY in (r.row_data or {})
            and not _fila_con_contenido(
                {k: v for k, v in (r.row_data or {}).items() if k != ROW_REF_KEY}
            )
        ]

        mode = "APPLY" if args.apply else "DRY-RUN"
        print(
            f"[{mode}] tenant {tid} — {len(rows)} PENDING revisadas, "
            f"{len(candidates)} vacías sin vínculo (candidatas a borrar), "
            f"{len(skipped_linked)} vacías pero CON vínculo humano (preservadas)."
        )

        by_group: dict[tuple[str | None, str | None], int] = {}
        for r in candidates:
            key = (str(r.uploaded_file_id) if r.uploaded_file_id else None, r.context_label)
            by_group[key] = by_group.get(key, 0) + 1
        for (file_id, label), n in sorted(by_group.items(), key=lambda kv: -kv[1])[:15]:
            print(f"    archivo={file_id or '—'}  motivo={label or '—'}  →  {n} fila(s)")
        if len(by_group) > 15:
            print(f"    ... y {len(by_group) - 15} grupo(s) más")

        if candidates:
            csv_path = args.export_csv or (
                f"dismiss_empty_unclassified_{tid}_"
                f"{datetime.now():%Y%m%d_%H%M%S}.csv"
            )
            with open(csv_path, "w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(["id", "uploaded_file_id", "context_label", "created_at"])
                for r in candidates:
                    writer.writerow([r.id, r.uploaded_file_id, r.context_label, r.created_at])
            print(f"\nCSV exportado: {csv_path} ({len(candidates)} fila(s)).")

        if not args.apply:
            await session.rollback()
            print("\nDry-run: nada se escribió.")
            await engine.dispose()
            return

        if not candidates:
            print("\nNada para borrar.")
            await engine.dispose()
            return

        ids = [r.id for r in candidates]
        result = await session.execute(
            text("DELETE FROM unclassified_records WHERE id = ANY(:ids)"),
            {"ids": ids},
        )
        await insert_decision_audit(
            session,
            tenant_id=str(tid),
            decision_type=_DECISION_TYPE,
            decision_data={
                "count": len(ids),
                "ids": [str(i) for i in ids],
                "skipped_linked": len(skipped_linked),
            },
            triggered_by="scripts/dismiss_empty_unclassified.py",
        )
        await session.commit()
        print(
            f"\nCOMMIT: {result.rowcount} fila(s) vacía(s) borradas físicamente. "
            f"Auditado en decision_audit_log ({_DECISION_TYPE})."
        )
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
