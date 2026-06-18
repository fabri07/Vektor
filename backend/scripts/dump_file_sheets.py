"""Re-parsea un archivo ya subido (desde S3 o local) y vuelca la estructura por
hoja/grupo, + qué columnas DETECTARÍA el importador en cada una.

Sirve para diagnosticar imports mal interpretados sin que el usuario resuba nada
(el archivo crudo vive en S3/R2). READ-ONLY: re-parsea en memoria, NO escribe DB.
NUNCA imprime la connection URL ni credenciales.

Uso:
    # desde S3 (necesita DATABASE_URL + credenciales S3/R2 en el env, como la app):
    DATABASE_URL='postgresql://...neon...?sslmode=require' \
        .venv/bin/python scripts/dump_file_sheets.py agustinalahora4@gmail.com
    DATABASE_URL='...' .venv/bin/python scripts/dump_file_sheets.py --file-id <uuid>

    # desde un archivo local (cero setup: ni DB ni S3) si todavía tenés el original:
    .venv/bin/python scripts/dump_file_sheets.py --local ~/Downloads/ASTERIA_home_deco.xlsx
"""

import asyncio
import json
import os
import sys

# Correr `scripts/dump_file_sheets.py` pone `scripts/` en sys.path[0] (así importa
# `_db`), pero deja `backend/` (donde vive `app/`) afuera. Lo agregamos para poder
# importar S3Client + el parser de la app.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DEFAULT_EMAIL = "agustinalahora4@gmail.com"
_BUCKETS = ("ventas_detectadas", "gastos_detectados", "stock_detectado", "otros_detectados")


def p(title: str) -> None:
    print(f"\n{'='*70}\n  {title}\n{'='*70}")


def _norm_headers(headers: list[str]) -> dict[str, str | None]:
    """Qué columnas detectaría el importador (estimación) sobre estos headers."""
    from app.application.services import ingestion_import_service as imp

    costo = imp._find_col(headers, imp._COSTO_UNITARIO_PRODUCT_COLS)
    precio = imp._resolve_sale_price_col(headers, costo)
    return {
        "nombre": imp._find_col(headers, imp._NOMBRE_COLS),
        "sku": imp._find_col(headers, imp._SKU_COLS),
        "precio_venta": precio,
        "costo_unitario": costo,
        "stock": imp._find_col(headers, imp._STOCK_COLS),
        "proveedor": imp._find_col(headers, imp._PROVEEDOR_COLS),
        "monto_venta": imp._find_col(headers, imp._VENTA_AMOUNT_COLS),
        "monto_gasto": imp._find_col(headers, imp._GASTO_AMOUNT_COLS),
        "fecha": imp._find_col(headers, imp._FECHA_COLS),
    }


def _dump_summary(summary: dict) -> None:
    p("RESUMEN DEL PARSEO")
    for k in (
        "file_type", "source_format", "inferred_type", "confidence",
        "multi_sheet", "has_venta", "has_gasto", "has_producto", "row_count",
    ):
        if k in summary:
            print(f"  {k}: {summary[k]}")

    contexts = summary.get("mapping_contexts") or []
    if contexts:
        p(f"MAPPING CONTEXTS ({len(contexts)} hojas/grupos)")
        for i, ctx in enumerate(contexts):
            headers = ctx.get("headers") or ctx.get("fields") or []
            print(f"\n  ── [{i}] {ctx.get('label')!r}  kind={ctx.get('source_kind')}  "
                  f"entity={ctx.get('entity_type')}  filas={ctx.get('row_count')}")
            print(f"     headers: {json.dumps(headers, ensure_ascii=False)}")
            if headers:
                det = _norm_headers([str(h) for h in headers])
                print(f"     detección estimada: "
                      f"{json.dumps({k: v for k, v in det.items() if v}, ensure_ascii=False)}")
            rows = ctx.get("preview_rows") or []
            if rows:
                print(f"     sample: {json.dumps(rows[0], ensure_ascii=False)[:300]}")
    else:
        p("BUCKETS DETECTADOS (sin mapping_contexts)")
        for bucket in _BUCKETS:
            rows = summary.get(bucket) or []
            print(f"\n  ── {bucket}: {len(rows)} filas")
            if rows:
                headers = list(rows[0].keys())
                print(f"     headers: {json.dumps(headers, ensure_ascii=False)}")
                det = _norm_headers([str(h) for h in headers])
                print(f"     detección estimada: "
                      f"{json.dumps({k: v for k, v in det.items() if v}, ensure_ascii=False)}")
                print(f"     sample: {json.dumps(rows[0], ensure_ascii=False)[:300]}")
        if not any(summary.get(b) for b in _BUCKETS):
            print("  (ninguno — el summary guardado pudo haberse compactado; "
                  "re-parseá desde S3/local)")


async def _from_db(
    email: str | None, tenant_arg: str | None, file_id: str | None
) -> tuple[bytes, str, str]:
    import asyncpg  # noqa: PLC0415
    from _db import normalize_dsn  # noqa: PLC0415

    raw = os.environ.get("DATABASE_URL") or os.environ.get("DATABASE_URL_SYNC")
    if not raw:
        print("ERROR: exportá DATABASE_URL (o usá --local <archivo>).")
        sys.exit(2)
    dsn, sslarg = normalize_dsn(raw)
    conn = await asyncpg.connect(dsn, ssl=sslarg)
    try:
        if file_id:
            row = await conn.fetchrow(
                "SELECT s3_key, content_type, original_filename "
                "FROM uploaded_files WHERE id = $1", file_id,
            )
        else:
            if tenant_arg:
                tid = tenant_arg
            else:
                tid = await conn.fetchval(
                    "SELECT tenant_id FROM users WHERE lower(email) = lower($1) LIMIT 1", email,
                )
                if not tid:
                    print(f"ERROR: no encontré tenant para {email}.")
                    sys.exit(2)
            row = await conn.fetchrow(
                "SELECT s3_key, content_type, original_filename FROM uploaded_files "
                "WHERE tenant_id = $1 ORDER BY created_at DESC LIMIT 1", tid,
            )
        if not row:
            print("ERROR: el tenant no tiene archivos subidos.")
            sys.exit(2)
    finally:
        await conn.close()

    print(f"  archivo: {row['original_filename']!r}  ({row['content_type']})")
    from app.integrations.s3 import S3Client  # noqa: PLC0415

    content = await S3Client().download(row["s3_key"])
    return content, row["content_type"], row["original_filename"]


async def main() -> None:
    args = sys.argv[1:]
    local_path: str | None = None
    file_id: str | None = None
    tenant_arg: str | None = None
    email: str | None = None
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--local":
            local_path = args[i + 1]
            i += 2
        elif a == "--file-id":
            file_id = args[i + 1]
            i += 2
        elif a == "--tenant":
            tenant_arg = args[i + 1]
            i += 2
        else:
            email = a
            i += 1
    if email is None and tenant_arg is None and file_id is None and local_path is None:
        email = DEFAULT_EMAIL

    if local_path:
        with open(local_path, "rb") as fh:
            content = fh.read()
        fname = os.path.basename(local_path)
        ctype = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" \
            if fname.lower().endswith(("xlsx", "xls")) else "text/csv"
        print(f"  archivo local: {fname!r}  ({ctype})")
    else:
        content, ctype, fname = await _from_db(email, tenant_arg, file_id)

    from app.application.services.file_parsing import parse_uploaded_content  # noqa: PLC0415

    summary = parse_uploaded_content(content, ctype, fname)
    _dump_summary(summary)


if __name__ == "__main__":
    asyncio.run(main())
