"""Diagnóstico read-only de "Tienda" y del estado de Asteria antes de diseñar
Bloque 2 (Tienda → proveedor).

SOLO corre SELECT contra Neon (via asyncpg) y SOLO descarga (S3Client.download,
nunca upload/delete) el Excel real desde R2. No escribe en Neon, R2 ni ninguna
variable de producción. No toca los runs de relectura trabados: solo los lee.

Uso:
    DATABASE_URL='postgresql://...neon.tech/vektor?sslmode=require' \
    S3_ENDPOINT_URL='https://<account>.r2.cloudflarestorage.com' \
    S3_ACCESS_KEY_ID='...' \
    S3_SECRET_ACCESS_KEY='...' \
    S3_BUCKET_NAME='vektor-uploads' \
        .venv/bin/python scripts/diag_asteria_tienda_readonly.py agustinalahora4@gmail.com

Si se omiten las variables S3_*, el script igual corre la parte de Neon y
salta la sección de preview del Excel real (avisa por qué).

Nunca imprime la connection URL ni las credenciales S3 (salen de _db.py /
app.config.settings, nunca se loguean acá).
"""

import asyncio
import json
import os
import sys
import unicodedata
from collections import defaultdict
from typing import Any

import asyncpg
from _db import normalize_dsn

# `app` importable desde scripts/ (sys.path[0] es el dir del script, no el CWD).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DEFAULT_EMAIL = "agustinalahora4@gmail.com"


def p(title: str) -> None:
    print(f"\n{'=' * 70}\n  {title}\n{'=' * 70}")


def _summary_dict(raw: object) -> dict[str, Any] | None:
    if raw is None:
        return None
    try:
        s = json.loads(raw) if isinstance(raw, str) else raw
    except Exception:  # noqa: BLE001
        return None
    return s if isinstance(s, dict) else None


def _fold(value: str) -> str:
    """Sin acentos, sin mayúsculas, espacios colapsados — para agrupar
    variantes de escritura del mismo nombre (tildes/mayúsculas/espacios)."""
    nfkd = unicodedata.normalize("NFKD", value)
    stripped = "".join(c for c in nfkd if not unicodedata.combining(c))
    return " ".join(stripped.lower().split())


async def main() -> None:
    args = sys.argv[1:]
    tenant_arg: str | None = None
    email: str | None = None
    if args and args[0] == "--tenant":
        if len(args) < 2:
            print("ERROR: --tenant requiere un UUID.")
            sys.exit(2)
        tenant_arg = args[1]
    elif args:
        email = args[0]
    else:
        email = DEFAULT_EMAIL

    raw = os.environ.get("DATABASE_URL") or os.environ.get("DATABASE_URL_SYNC")
    if not raw:
        print("ERROR: exportá DATABASE_URL con la URL de Neon antes de correr.")
        sys.exit(2)
    dsn, sslarg = normalize_dsn(raw)
    conn = await asyncpg.connect(dsn, ssl=sslarg)
    try:
        await run(conn, email=email, tenant_arg=tenant_arg)
    finally:
        await conn.close()


async def resolve_tenant(
    conn: asyncpg.Connection, *, email: str | None, tenant_arg: str | None
) -> str | None:
    if tenant_arg:
        p(f"TENANT (por --tenant)  {tenant_arg}")
        t = await conn.fetchrow("SELECT * FROM tenants WHERE tenant_id = $1", tenant_arg)
        if not t:
            print("  ⚠ No existe ningún tenant con ese tenant_id.")
            return None
        return str(tenant_arg)

    assert email is not None
    p(f"USUARIO / TENANT  ({email})")
    users = await conn.fetch(
        "SELECT user_id, tenant_id, email FROM users WHERE lower(email) = lower($1)",
        email,
    )
    if not users:
        print("  ⚠ No existe ningún usuario con ese email.")
        return None
    tid = users[0]["tenant_id"]
    print(f"  tenant_id={tid}")
    return str(tid)


# ── 1/2/7. Tienda: valores, frecuencia, colisiones de normalización ──────────
async def tienda_en_productos(conn: asyncpg.Connection, tid: str) -> None:
    p("TIENDA — valores persistidos hoy en products.custom_fields")
    rows = await conn.fetch(
        "SELECT custom_fields->>'marca' AS marca, count(*) AS n "
        "FROM products WHERE tenant_id=$1 AND deactivated_at IS NULL "
        "  AND custom_fields ? 'marca' "
        "GROUP BY 1 ORDER BY n DESC",
        tid,
    )
    if not rows:
        print("  Ningún producto activo tiene custom_fields['marca'] seteado hoy.")
        return
    print(f"  {len(rows)} valores distintos (original → cantidad de productos):")
    by_norm: dict[str, list[str]] = defaultdict(list)
    for r in rows:
        print(f"     {r['marca']!r}: {r['n']}")
        by_norm[_fold(r["marca"] or "")].append(r["marca"])
    print("\n  COLISIONES por tilde/mayúscula/espacio (mismo valor normalizado, "
          "distinta escritura):")
    colisiones = {k: v for k, v in by_norm.items() if len(set(v)) > 1}
    if not colisiones:
        print("     ninguna detectada sobre lo ya persistido.")
    for norm, variantes in colisiones.items():
        print(f"     {norm!r} ← {sorted(set(variantes))}")


# ── 3. Coincidencias con proveedores existentes ──────────────────────────────
async def coincidencias_con_suppliers(conn: asyncpg.Connection, tid: str) -> None:
    from app.application.services.ingestion_import_service import (  # noqa: PLC0415
        _normalize_supplier_name,
    )

    p("COINCIDENCIAS Tienda ↔ Suppliers existentes")
    marcas = await conn.fetch(
        "SELECT DISTINCT custom_fields->>'marca' AS marca FROM products "
        "WHERE tenant_id=$1 AND deactivated_at IS NULL AND custom_fields ? 'marca'",
        tid,
    )
    suppliers = await conn.fetch(
        "SELECT id, name FROM suppliers WHERE tenant_id=$1 AND deactivated_at IS NULL "
        "  AND coalesce(custom_fields->>'_sentinel', '') <> 'true'",
        tid,
    )
    if not marcas:
        print("  No hay valores de Tienda persistidos para comparar.")
        return
    by_norm_supplier = {_normalize_supplier_name(s["name"] or ""): s for s in suppliers}
    print(f"  {len(suppliers)} proveedores reales (no sentinela) en el tenant.")
    for m in marcas:
        marca = m["marca"] or ""
        norm = _normalize_supplier_name(marca)
        hit = by_norm_supplier.get(norm)
        if hit:
            print(f"     {marca!r} → YA existe como Supplier {hit['name']!r} (id={hit['id']})")
        else:
            print(f"     {marca!r} → sin proveedor existente que matchee")


# ── 4. Productos que comparten nombre pero tienen distinta Tienda ───────────
async def productos_mismo_nombre_distinta_tienda(conn: asyncpg.Connection, tid: str) -> None:
    p("PRODUCTOS con el mismo nombre y distinta Tienda")
    rows = await conn.fetch(
        "SELECT name_normalized, "
        "       array_agg(DISTINCT custom_fields->>'marca') AS marcas, "
        "       count(*) AS n "
        "FROM products "
        "WHERE tenant_id=$1 AND deactivated_at IS NULL AND custom_fields ? 'marca' "
        "GROUP BY name_normalized "
        "HAVING count(DISTINCT custom_fields->>'marca') > 1 "
        "ORDER BY n DESC LIMIT 30",
        tid,
    )
    if not rows:
        print("  Ninguno detectado sobre custom_fields['marca'] persistido.")
        return
    for r in rows:
        print(f"     {r['name_normalized']!r} ({r['n']} filas): {r['marcas']}")


# ── 5. Relación producto↔proveedor YA existente en el modelo ────────────────
async def relacion_producto_proveedor_existente(conn: asyncpg.Connection, tid: str) -> None:
    p("RELACIÓN Producto↔Supplier ya existente en el modelo (inventory_movements)")
    rows = await conn.fetchrow(
        "SELECT count(*) AS movimientos_con_supplier, "
        "       count(DISTINCT product_id) FILTER (WHERE supplier_id IS NOT NULL) "
        "         AS productos_con_supplier, "
        "       count(DISTINCT supplier_id) FILTER (WHERE supplier_id IS NOT NULL) "
        "         AS suppliers_distintos "
        "FROM inventory_movements WHERE tenant_id=$1 AND supplier_id IS NOT NULL",
        tid,
    )
    print(f"  movimientos con supplier_id: {rows['movimientos_con_supplier']}")
    print(f"  productos distintos vinculados a algún supplier: {rows['productos_con_supplier']}")
    print(f"  suppliers distintos referenciados: {rows['suppliers_distintos']}")
    print("  → hoy NO hay FK directa Product→Supplier: el vínculo vive en "
          "inventory_movements.supplier_id (evidencia de compra), o en "
          "expense_entries.supplier_id/supplier_name para gastos.")
    gastos = await conn.fetchrow(
        "SELECT count(*) FILTER (WHERE supplier_id IS NOT NULL) AS con_supplier_id, "
        "       count(*) FILTER (WHERE supplier_id IS NULL AND supplier_name IS NOT NULL) "
        "         AS solo_nombre "
        "FROM expense_entries WHERE tenant_id=$1 AND voided_at IS NULL "
        "  AND (supplier_id IS NOT NULL OR supplier_name IS NOT NULL)",
        tid,
    )
    print(f"  gastos con supplier_id: {gastos['con_supplier_id']}  "
          f"· gastos solo con supplier_name (texto libre): {gastos['solo_nombre']}")


# ── 8. Runs de relectura trabados — SOLO LECTURA ─────────────────────────────
async def runs_trabados(conn: asyncpg.Connection, tid: str) -> None:
    p("RUNS DE RELECTURA (data_repair_runs, repair_type=REREAD_FILE) — sin tocar")
    rows = await conn.fetch(
        "SELECT id, status, dry_run, source_run_id, created_at, updated_at, "
        "       queued_at, completed_at, details_json "
        "FROM data_repair_runs "
        "WHERE tenant_id=$1 AND repair_type='REREAD_FILE' "
        "ORDER BY created_at DESC LIMIT 20",
        tid,
    )
    if not rows:
        print("  No hay runs de relectura para este tenant.")
        return
    for r in rows:
        details = _summary_dict(r["details_json"]) or {}
        error = details.get("error") or details.get("failure_reason") or details.get("stage")
        print(
            f"  id={r['id']}  status={r['status']}  dry_run={r['dry_run']}  "
            f"created={r['created_at']}  updated={r['updated_at']}  "
            f"queued_at={r['queued_at']}  completed_at={r['completed_at']}"
        )
        if r["status"] in ("RUNNING", "QUEUED", "APPLYING"):
            print(f"     ⚠ TRABADO/en curso — causa registrada en details_json: {error!r}")
            print(f"     details_json keys: {list(details.keys())}")


# ── Archivo real: identificar + descargar de R2 (solo lectura) ──────────────
async def encontrar_archivo_asteria(conn: asyncpg.Connection, tid: str) -> dict[str, Any] | None:
    p("UPLOADED FILES del tenant (para ubicar el Excel real)")
    files = await conn.fetch(
        "SELECT id, original_filename, s3_key, content_type, purpose, status, "
        "processing_status, created_at "
        "FROM uploaded_files WHERE tenant_id=$1 ORDER BY created_at DESC",
        tid,
    )
    for f in files:
        print(
            f"  {f['original_filename']!r}  id={f['id']}  purpose={f['purpose']}  "
            f"status={f['status']}/{f['processing_status']}  {f['created_at']}"
        )
    candidatos = [f for f in files if "asteria" in (f["original_filename"] or "").lower()]
    if not candidatos:
        candidatos = [f for f in files if f["purpose"] == "ingestion"]
    if not candidatos:
        print("  ⚠ No se encontró un archivo candidato.")
        return None
    elegido = candidatos[0]
    print(f"\n  Elegido para preview: {elegido['original_filename']!r} (id={elegido['id']})")
    return dict(elegido)


async def preview_con_bloque_1(archivo: dict[str, Any]) -> None:
    p("PREVIEW DEL EXCEL REAL con el Bloque 1 aplicado (parser local, sin escribir nada)")
    faltan = [
        v
        for v in ("S3_ENDPOINT_URL", "S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY", "S3_BUCKET_NAME")
        if not os.environ.get(v)
    ]
    if faltan:
        print(f"  ⚠ Salteado: faltan variables S3 ({', '.join(faltan)}). "
              "Exportalas para incluir esta sección.")
        return

    from app.application.services.file_parsing import parse_uploaded_content  # noqa: PLC0415
    from app.integrations.s3 import S3Client  # noqa: PLC0415

    s3 = S3Client()
    content = await s3.download(archivo["s3_key"])
    print(f"  Descargado read-only: {len(content)} bytes de {archivo['s3_key']!r}")

    summary = parse_uploaded_content(
        content, archivo["content_type"] or "application/octet-stream", archivo["original_filename"]
    )

    contexts = summary.get("mapping_contexts") or []
    print(f"\n  {len(contexts)} hojas/contextos detectados:")
    for ctx in contexts:
        derived = " [DERIVADA — excluida por Bloque 1]" if ctx.get("is_summary_or_derived") else ""
        print(
            f"     {ctx.get('label')!r}: entity_type={ctx.get('entity_type')} "
            f"row_count={ctx.get('row_count')}{derived}"
        )

    for bucket in (
        "derived_detected",
        "otros_detectados",
        "ventas_detectadas",
        "gastos_detectados",
        "stock_detectado",
        "clientes_detectados",
        "proveedores_detectados",
    ):
        rows = summary.get(bucket) or []
        print(f"  {bucket}: {len(rows)} filas")

    if summary.get("warnings"):
        print("\n  warnings del parser:")
        for w in summary["warnings"]:
            print(f"     - {w}")


async def run(conn: asyncpg.Connection, *, email: str | None, tenant_arg: str | None) -> None:
    tid = await resolve_tenant(conn, email=email, tenant_arg=tenant_arg)
    if tid is None:
        return

    await tienda_en_productos(conn, tid)
    await coincidencias_con_suppliers(conn, tid)
    await productos_mismo_nombre_distinta_tienda(conn, tid)
    await relacion_producto_proveedor_existente(conn, tid)
    await runs_trabados(conn, tid)
    archivo = await encontrar_archivo_asteria(conn, tid)
    if archivo:
        await preview_con_bloque_1(archivo)


if __name__ == "__main__":
    asyncio.run(main())
