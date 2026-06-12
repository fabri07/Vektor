"""Reparación in-place de ventas importadas con fecha colapsada al día del import.

Caso: el pipeline viejo (pre 2026-06-06) ignoraba la columna `fecha` del archivo
y grababa todas las ventas con transaction_date = día del import. El crudo en R2
tiene la fecha+hora real por fila, más el SKU para vincular product_id.

Usage:
    # Dry-run (default): muestra qué cambiaría, no escribe nada.
    DATABASE_URL='postgresql://...' .venv/bin/python scripts/repair_sale_dates_from_file.py \
        --tenant <uuid> --filename operaciones_kiosco_beta.xlsx --created-on 2026-06-05

    # Aplicar:
    ... --apply

Qué hace:
  1. Descarga el crudo de R2 y lo re-parsea con el pipeline ACTUAL.
  2. Verifica que las ventas VIVAS del tenant creadas el día del import matcheen
     1:1 con las filas de ventas del archivo por (producto, cantidad, monto).
     Si el multiset no coincide exactamente, ABORTA sin escribir nada.
  3. Asigna a cada venta la fecha+hora real de su fila (asignación estable
     dentro de cada grupo: filas DB por id, filas archivo en orden de aparición)
     y vincula product_id resolviendo el SKU contra el catálogo del tenant.
  4. Reversible: la fecha anterior queda en
     custom_fields.repair_prev_transaction_date.

NUNCA imprime la connection URL. Requiere correr desde backend/.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid
from collections import Counter, defaultdict, deque
from datetime import date, datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _db import async_engine_config  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine  # noqa: E402

from app.application.services.file_parsing import parse_uploaded_content  # noqa: E402
from app.integrations.s3 import S3Client  # noqa: E402

_DATE_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
    "%d/%m/%Y %H:%M:%S",
    "%d/%m/%Y %H:%M",
    "%d/%m/%Y",
)


def _parse_fecha(raw: object) -> datetime | None:
    if isinstance(raw, datetime):
        return raw
    s = str(raw or "").strip()
    if not s:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _row_key(producto: object, cantidad: object, monto: object) -> tuple[str, int, str]:
    try:
        qty = int(float(cantidad or 0))
    except (TypeError, ValueError):
        qty = 0
    return (str(producto or "").strip(), qty, f"{float(monto):.2f}")


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--filename", required=True)
    parser.add_argument(
        "--created-on",
        required=True,
        type=date.fromisoformat,
        help="created_at::date de las ventas rotas (día del import)",
    )
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    tid = uuid.UUID(args.tenant)

    url, connect_args = async_engine_config()
    engine = create_async_engine(url, connect_args=connect_args)
    async with AsyncSession(engine) as session:
        file_row = (
            await session.execute(
                text(
                    "SELECT id, content_type, s3_key FROM uploaded_files "
                    "WHERE tenant_id = :tid AND original_filename = :fn "
                    "AND deleted_at IS NULL AND s3_key IS NOT NULL"
                ),
                {"tid": str(tid), "fn": args.filename},
            )
        ).first()
        if file_row is None:
            print(f"ERROR: no se encontró {args.filename!r} para el tenant.")
            sys.exit(2)

        content = await S3Client().download(file_row.s3_key)
        summary = parse_uploaded_content(content, file_row.content_type, args.filename)
        ventas_archivo = summary.get("ventas_detectadas") or []
        if not ventas_archivo:
            print("ERROR: el re-parseo no detectó ventas en el archivo.")
            sys.exit(2)

        # Filas del archivo en orden de aparición, agrupadas por clave de match.
        archivo_por_clave: dict[tuple[str, int, str], deque] = defaultdict(deque)
        sin_fecha = 0
        for row in ventas_archivo:
            fecha = _parse_fecha(row.get("fecha"))
            if fecha is None:
                sin_fecha += 1
                continue
            key = _row_key(row.get("producto"), row.get("cantidad"), row.get("total"))
            archivo_por_clave[key].append((fecha, str(row.get("sku") or "").strip()))
        if sin_fecha:
            print(f"ERROR: {sin_fecha} fila(s) del archivo sin fecha parseable. Abortando.")
            sys.exit(2)

        db_rows = (
            await session.execute(
                text(
                    "SELECT id, notes, quantity, amount, transaction_date "
                    "FROM sales_entries WHERE tenant_id = :tid AND voided_at IS NULL "
                    "AND created_at::date = :dia ORDER BY id"
                ),
                {"tid": str(tid), "dia": args.created_on},
            )
        ).all()

        # Verificación 1:1 estricta por multiset (producto, cantidad, monto).
        file_ms = Counter(
            {k: len(v) for k, v in archivo_por_clave.items()}
        )
        db_ms = Counter(_row_key(r.notes, r.quantity, r.amount) for r in db_rows)
        solo_archivo = file_ms - db_ms
        solo_db = db_ms - file_ms
        if solo_archivo or solo_db:
            print(
                f"ERROR: multiset no coincide — solo_archivo={sum(solo_archivo.values())} "
                f"solo_db={sum(solo_db.values())}. Abortando sin escribir."
            )
            for k, v in list(solo_archivo.items())[:5]:
                print("   archivo:", k, v)
            for k, v in list(solo_db.items())[:5]:
                print("   db:", k, v)
            sys.exit(2)

        sku_a_producto = {
            (r.sku or "").strip(): r.id
            for r in (
                await session.execute(
                    text("SELECT id, sku FROM products WHERE tenant_id = :tid"),
                    {"tid": str(tid)},
                )
            ).all()
            if (r.sku or "").strip()
        }

        updates: list[dict[str, object]] = []
        fechas: list[datetime] = []
        sin_producto = 0
        for r in db_rows:
            fecha, sku = archivo_por_clave[_row_key(r.notes, r.quantity, r.amount)].popleft()
            product_id = sku_a_producto.get(sku)
            if product_id is None:
                sin_producto += 1
            fechas.append(fecha)
            updates.append(
                {
                    "id": r.id,
                    "dt": fecha,
                    "pid": product_id,
                    "prev": json.dumps(
                        {"repair_prev_transaction_date": r.transaction_date.isoformat()}
                    ),
                }
            )

        dias = {f.date() for f in fechas}
        modo = "APPLY" if args.apply else "DRY-RUN"
        print(
            f"[{modo}] tenant {tid} — {len(updates)} venta(s): "
            f"fechas {min(fechas)} .. {max(fechas)} ({len(dias)} días distintos), "
            f"sin product_id={sin_producto}"
        )
        for u in updates[:5]:
            print(f"    {u['id']}  → {u['dt']}  product={'sí' if u['pid'] else '—'}")

        if not args.apply:
            print("Dry-run: nada se escribió.")
        else:
            await session.execute(
                text(
                    "UPDATE sales_entries SET transaction_date = :dt, "
                    "product_id = COALESCE(CAST(:pid AS UUID), product_id), "
                    "custom_fields = COALESCE(custom_fields, '{}'::jsonb) || "
                    "CAST(:prev AS JSONB), updated_at = now() "
                    "WHERE id = :id"
                ),
                updates,
            )
            await session.commit()
            print(
                "COMMIT: fechas y product_id reparados. Reversible con "
                "custom_fields->>'repair_prev_transaction_date'."
            )
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
