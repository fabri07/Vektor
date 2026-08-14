"""Read-only diagnostic of the import/data state for one tenant (ASTERIA u otro).

Usage:
    DATABASE_URL='postgresql://...neon.tech/vektor?sslmode=require' \
        .venv/bin/python scripts/diag_asteria_import.py agustinalahora4@gmail.com
    DATABASE_URL='...' .venv/bin/python scripts/diag_asteria_import.py --tenant <uuid>

ONLY runs SELECT statements. No writes. Safe against production.
NUNCA imprime la connection URL ni la DATABASE_URL (sale de _db.py / el shell).

## Paso 0 del plan de ingesta (2026-08-14) — este script es una COMPUERTA

Además del estado general, mide las tres cosas de las que dependen las fases nuevas.
Ninguna limpieza ni backfill se ejecuta antes de leer esta salida:

- **F-S / F-CAT** — cuánto del catálogo tiene SKU, barcode y categoría propios. Es la
  línea de base del backfill y lo que decide cuántos productos nacerían ``GEN-xxxx``.
- **F-S.0** — cuántos NOMBRES distintos hay entre las ventas sin producto vinculado.
  Ése es el tamaño real de la cola de vinculación: resolver N nombres una vez arregla
  las N×k filas que los repiten. Más ``_customer_resolution`` y el conteo de maestros,
  que dicen si el problema de clientes/proveedores es de datos o de contrato (F-E).
- **F-O.3 / F-O.4** — el reparto de los pendientes de «Otros» por motivo × archivo, y
  cuántos están REALMENTE vacíos. Si "todo vacío" no domina, la hipótesis de F-O.4 se
  cae y la fase se rediseña en vez de forzarse.

El tercer corte del Paso 0 no vive acá porque ya existe: la duplicación del catálogo la
mide el dry-run del dedup, que persiste el plan sin tocar negocio —

    DATABASE_URL='...' .venv/bin/python scripts/dedupe_products_by_name.py \
        --tenant <uuid> --out plan.csv
"""

import asyncio
import json
import os
import sys
from collections import Counter

import asyncpg
from _db import normalize_dsn

DEFAULT_EMAIL = "agustinalahora4@gmail.com"


def p(title: str) -> None:
    print(f"\n{'='*70}\n  {title}\n{'='*70}")


def _summary_dict(raw: object) -> dict | None:
    """parsed_summary_json puede venir como str (JSONB serializado) o dict."""
    if raw is None:
        return None
    try:
        s = json.loads(raw) if isinstance(raw, str) else raw
    except Exception:  # noqa: BLE001
        return None
    return s if isinstance(s, dict) else None


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
        print("  " + ", ".join(f"{k}={v}" for k, v in dict(t).items() if k != "tenant_id")[:400])
        return str(tenant_arg)

    assert email is not None
    p(f"USUARIO / TENANT  ({email})")
    users = await conn.fetch(
        "SELECT user_id, tenant_id, email, role_code, is_active, last_login_at "
        "FROM users WHERE lower(email) = lower($1)",
        email,
    )
    if not users:
        print("  ⚠ No existe ningún usuario con ese email.")
        like = await conn.fetch(
            "SELECT email FROM users WHERE email ILIKE $1 LIMIT 10", f"%{email.split('@')[0]}%"
        )
        if like:
            print("  Parecidos:", [r["email"] for r in like])
        return None
    for u in users:
        print(
            f"  user_id={u['user_id']}  tenant_id={u['tenant_id']}  role={u['role_code']} "
            f"active={u['is_active']}  last_login={u['last_login_at']}"
        )
    tenant_ids = list({u["tenant_id"] for u in users})
    if len(tenant_ids) > 1:
        print(f"  ⚠ Email asociado a {len(tenant_ids)} tenants; uso el primero. "
              f"Pasá --tenant <uuid> para elegir.")
    tid = tenant_ids[0]
    t = await conn.fetchrow("SELECT * FROM tenants WHERE tenant_id = $1", tid)
    if t:
        print("\n  TENANT " + ", ".join(
            f"{k}={v}" for k, v in dict(t).items() if k != "tenant_id"
        )[:400])
    return str(tid)


async def run(
    conn: asyncpg.Connection, *, email: str | None, tenant_arg: str | None
) -> None:
    tid = await resolve_tenant(conn, email=email, tenant_arg=tenant_arg)
    if tid is None:
        return

    bp = await conn.fetchrow(
        "SELECT vertical_code, onboarding_completed FROM business_profiles "
        "WHERE tenant_id=$1 LIMIT 1",
        tid,
    )
    if bp:
        print(f"  business: vertical={bp['vertical_code']} onboarding={bp['onboarding_completed']}")

    # ── PRODUCTOS ────────────────────────────────────────────────────────────
    p(f"PRODUCTOS  (tenant {tid})")
    prod = await conn.fetchrow(
        "SELECT "
        "  count(*) FILTER (WHERE is_active AND deactivated_at IS NULL) AS activos, "
        "  count(*) FILTER (WHERE unit_cost_ars IS NULL "
        "                   AND deactivated_at IS NULL) AS sin_costo, "
        "  count(*) FILTER (WHERE requires_completion "
        "                   AND deactivated_at IS NULL) AS requires_completion, "
        "  count(*) FILTER (WHERE sale_price_ars = 0 "
        "                   AND deactivated_at IS NULL) AS precio_cero, "
        "  count(*) FILTER (WHERE deactivated_at IS NOT NULL) AS desactivados, "
        "  count(*) AS total "
        "FROM products WHERE tenant_id=$1",
        tid,
    )
    print(f"  activos (is_active, no desactivados): {prod['activos']}")
    print(f"  unit_cost_ars NULL (no desactivados): {prod['sin_costo']}")
    print(f"  requires_completion=True (no desactivados): {prod['requires_completion']}")
    print(f"  sale_price_ars = 0 (no desactivados): {prod['precio_cero']}")
    print(f"  desactivados (deactivated_at IS NOT NULL): {prod['desactivados']}")
    print(f"  total filas: {prod['total']}")

    # ── IDENTIDAD DE PRODUCTO (F-S / F-S.0) ──────────────────────────────────
    # Cuánto del catálogo tiene código propio hoy: es la línea de base del
    # backfill de SKU y lo que decide cuántos van a nacer con prefijo GEN.
    ident = await conn.fetchrow(
        "SELECT "
        "  count(*) AS activos, "
        "  count(*) FILTER (WHERE sku IS NOT NULL AND btrim(sku) <> '') AS con_sku, "
        "  count(*) FILTER (WHERE barcode IS NOT NULL AND btrim(barcode) <> '') AS con_barcode, "
        "  count(*) FILTER (WHERE category IS NOT NULL AND btrim(category) <> '') AS con_categoria "
        "FROM products WHERE tenant_id=$1 AND is_active AND deactivated_at IS NULL",
        tid,
    )
    _act = ident["activos"] or 1
    print(f"\n  IDENTIDAD (sobre {ident['activos']} activos):")
    print(f"    con SKU: {ident['con_sku']}  ({100 * ident['con_sku'] // _act}%)")
    print(f"    con barcode: {ident['con_barcode']}")
    print(f"    con categoría: {ident['con_categoria']}  "
          f"→ sin categoría: {ident['activos'] - ident['con_categoria']} "
          f"(nacerían GEN-xxxx si se numera antes de F-CAT)")
    pcat = await conn.fetch(
        "SELECT coalesce(nullif(btrim(category), ''), '(sin categoría)') AS category, "
        "       count(*) AS n "
        "FROM products WHERE tenant_id=$1 AND is_active AND deactivated_at IS NULL "
        "GROUP BY 1 ORDER BY n DESC LIMIT 20",
        tid,
    )
    print("    distribución por category (top 20):")
    for r in pcat:
        print(f"       {r['category']!r}: {r['n']}")

    # ── STOCK VALORIZADO ─────────────────────────────────────────────────────
    p(f"STOCK VALORIZADO  (tenant {tid})")
    stock = await conn.fetchrow(
        "SELECT "
        "  coalesce(sum(stock_units * unit_cost_ars) "
        "    FILTER (WHERE unit_cost_ars IS NOT NULL), 0) AS valorizado, "
        "  count(*) FILTER (WHERE stock_units > 0 "
        "                   AND unit_cost_ars IS NULL) AS missing_cost_count, "
        "  coalesce(sum(stock_units) "
        "    FILTER (WHERE stock_units > 0 AND unit_cost_ars IS NULL), 0) AS missing_cost_units "
        "FROM products WHERE tenant_id=$1 AND deactivated_at IS NULL",
        tid,
    )
    print(f"  stock valorizado (Σ stock_units × unit_cost_ars, costo no NULL): "
          f"{stock['valorizado']}")
    print(f"  productos con stock>0 y unit_cost NULL (missing_cost): "
          f"{stock['missing_cost_count']} productos / {stock['missing_cost_units']} unidades")

    # ── VENTAS ───────────────────────────────────────────────────────────────
    p(f"VENTAS  (sales_entries, voided_at IS NULL)  (tenant {tid})")
    ventas = await conn.fetchrow(
        "SELECT count(*) AS total, "
        "  count(*) FILTER (WHERE product_id IS NULL) AS sin_product "
        "FROM sales_entries WHERE tenant_id=$1 AND voided_at IS NULL",
        tid,
    )
    print(f"  total: {ventas['total']}")
    print(f"  con product_id NULL: {ventas['sin_product']}")

    # ── F-S.0: el tamaño REAL del trabajo de vinculación ─────────────────────
    # No son las filas sin producto: son los NOMBRES distintos que hay entre
    # ellas. Resolver N nombres una vez arregla las N*k filas que los repiten.
    nombres = await conn.fetchrow(
        "SELECT count(DISTINCT lower(btrim(notes))) AS distintos "
        "FROM sales_entries "
        "WHERE tenant_id=$1 AND voided_at IS NULL AND product_id IS NULL "
        "  AND notes IS NOT NULL AND btrim(notes) <> ''",
        tid,
    )
    print(f"  → nombres DISTINTOS entre las ventas sin producto: {nombres['distintos']} "
          f"(es el tamaño real de la cola de F-S.0)")
    top_nombres = await conn.fetch(
        "SELECT lower(btrim(notes)) AS nombre, count(*) AS n "
        "FROM sales_entries "
        "WHERE tenant_id=$1 AND voided_at IS NULL AND product_id IS NULL "
        "  AND notes IS NOT NULL AND btrim(notes) <> '' "
        "GROUP BY 1 ORDER BY n DESC LIMIT 15",
        tid,
    )
    if top_nombres:
        print("  top 15 nombres sin vincular (nombre → cuántas ventas):")
        for r in top_nombres:
            print(f"     {r['nombre']!r}: {r['n']}")

    # ── F-E / F-I: cómo resolvió el cliente cada venta importada ─────────────
    # matched = vinculó / anonymous = la fila no traía referencia (mostrador) /
    # unresolved = traía una y no matcheó → «Local» con el dato crudo guardado.
    cres = await conn.fetch(
        "SELECT coalesce(custom_fields->>'_customer_resolution', '(sin marca)') AS r, "
        "       count(*) AS n "
        "FROM sales_entries WHERE tenant_id=$1 AND voided_at IS NULL "
        "GROUP BY 1 ORDER BY n DESC",
        tid,
    )
    print("  resolución de cliente (_customer_resolution):")
    for r in cres:
        print(f"     {r['r']}: {r['n']}")
    maestros = await conn.fetchrow(
        "SELECT "
        "  (SELECT count(*) FROM customers WHERE tenant_id=$1) AS clientes, "
        "  (SELECT count(*) FROM customers WHERE tenant_id=$1 "
        "     AND coalesce(custom_fields->>'_sentinel','') = 'true') AS clientes_centinela, "
        "  (SELECT count(*) FROM suppliers WHERE tenant_id=$1) AS proveedores, "
        "  (SELECT count(*) FROM suppliers WHERE tenant_id=$1 "
        "     AND coalesce(custom_fields->>'_sentinel','') = 'true') AS prov_centinela",
        tid,
    )
    print(f"  maestros: clientes={maestros['clientes']} "
          f"(centinela «Local»: {maestros['clientes_centinela']}) · "
          f"proveedores={maestros['proveedores']} "
          f"(centinela «No identificado»: {maestros['prov_centinela']})")

    # ── GASTOS ───────────────────────────────────────────────────────────────
    p(f"GASTOS  (expense_entries, voided_at IS NULL)  (tenant {tid})")
    gastos = await conn.fetchrow(
        "SELECT count(*) AS total, "
        "  count(*) FILTER (WHERE product_id IS NULL) AS sin_product, "
        "  count(*) FILTER (WHERE supplier_id IS NULL) AS sin_supplier_id, "
        "  count(*) FILTER (WHERE supplier_name IS NULL) AS sin_supplier_name "
        "FROM expense_entries WHERE tenant_id=$1 AND voided_at IS NULL",
        tid,
    )
    print(f"  total: {gastos['total']}")
    print(f"  con product_id NULL: {gastos['sin_product']}")
    print(f"  con supplier_id NULL: {gastos['sin_supplier_id']}")
    print(f"  con supplier_name NULL: {gastos['sin_supplier_name']}")
    cat = await conn.fetch(
        "SELECT category, count(*) AS n "
        "FROM expense_entries WHERE tenant_id=$1 AND voided_at IS NULL "
        "GROUP BY category ORDER BY n DESC LIMIT 20",
        tid,
    )
    print("  distribución por category (top 20):")
    for r in cat:
        print(f"     {r['category']!r}: {r['n']}")

    # ── UNCLASSIFIED RECORDS ─────────────────────────────────────────────────
    p(f"UNCLASSIFIED RECORDS  (tenant {tid})")
    by_entity = await conn.fetch(
        "SELECT suggested_entity, count(*) AS n "
        "FROM unclassified_records WHERE tenant_id=$1 "
        "GROUP BY suggested_entity ORDER BY n DESC",
        tid,
    )
    by_status = await conn.fetch(
        "SELECT status, count(*) AS n "
        "FROM unclassified_records WHERE tenant_id=$1 "
        "GROUP BY status ORDER BY n DESC",
        tid,
    )
    total_unc = sum(r["n"] for r in by_status)
    print(f"  total: {total_unc}")
    print("  por suggested_entity: " + ", ".join(
        f"{r['suggested_entity']}={r['n']}" for r in by_entity
    ))
    print("  por status: " + ", ".join(f"{r['status']}={r['n']}" for r in by_status))

    # ── F-O.3: POR QUÉ está cada fila acá ────────────────────────────────────
    # `context_label` lo setean los 19 sitios de captura y la pantalla no lo
    # muestra: 46 páginas de filas indistinguibles. Acá se ve el reparto real.
    por_motivo = await conn.fetch(
        "SELECT coalesce(u.context_label, '(sin motivo)') AS motivo, "
        "       coalesce(f.original_filename, '(sin archivo)') AS archivo, "
        "       count(*) AS n "
        "FROM unclassified_records u "
        "LEFT JOIN uploaded_files f ON f.id = u.uploaded_file_id "
        "WHERE u.tenant_id=$1 AND u.status='PENDING' "
        "GROUP BY 1, 2 ORDER BY n DESC LIMIT 30",
        tid,
    )
    print("\n  PENDIENTES por motivo × archivo (top 30):")
    for r in por_motivo:
        print(f"     [{r['n']:>6}]  {r['archivo']}  ←  {r['motivo']}")

    # ── F-O.4 (COMPUERTA): ¿cuántos de estos pendientes están realmente vacíos?
    # Hipótesis a confirmar o refutar ANTES de escribir la limpieza: una fila
    # 100% en blanco del xlsx se captura igual, porque `_capture_unclassified`
    # chequea que el dict tenga CLAVES, no que sus valores tengan algo.
    # Las claves reservadas (prefijo `__`) no cuentan como contenido: son
    # metadata del sistema, no columnas del archivo.
    vacios = await conn.fetchrow(
        "WITH pend AS ("
        "  SELECT id, row_data FROM unclassified_records "
        "  WHERE tenant_id=$1 AND status='PENDING'"
        "), val AS ("
        "  SELECT p.id, kv.key AS k, coalesce(kv.value, '') AS v "
        "  FROM pend p, LATERAL jsonb_each_text(p.row_data) AS kv(key, value) "
        "  WHERE left(kv.key, 2) <> '__'"
        ") "
        "SELECT "
        "  (SELECT count(*) FROM pend) AS pendientes, "
        "  (SELECT count(*) FROM pend p WHERE NOT EXISTS ("
        "     SELECT 1 FROM val v WHERE v.id = p.id)) AS sin_columnas, "
        "  (SELECT count(*) FROM pend p WHERE EXISTS ("
        "     SELECT 1 FROM val v WHERE v.id = p.id) AND NOT EXISTS ("
        "     SELECT 1 FROM val v WHERE v.id = p.id "
        "       AND btrim(replace(v.v, chr(160), ' ')) <> '')) AS todo_vacio, "
        "  (SELECT count(*) FROM pend p WHERE EXISTS ("
        "     SELECT 1 FROM val v WHERE v.id = p.id "
        "       AND v.v <> '' AND btrim(replace(v.v, chr(160), ' ')) = '')"
        "   AND NOT EXISTS ("
        "     SELECT 1 FROM val v WHERE v.id = p.id "
        "       AND btrim(replace(v.v, chr(160), ' ')) <> '')) AS solo_invisibles",
        tid,
    )
    _p = vacios["pendientes"] or 1
    print("\n  CONTENIDO REAL de los pendientes (compuerta de F-O.4):")
    print(f"     pendientes: {vacios['pendientes']}")
    print(f"     sin ninguna columna propia (dict vacío o sólo claves __): "
          f"{vacios['sin_columnas']}")
    print(f"     con columnas pero TODOS los valores vacíos: {vacios['todo_vacio']}  "
          f"({100 * vacios['todo_vacio'] // _p}%)")
    print(f"        ↳ de ésos, los que traen sólo espacios/invisibles: "
          f"{vacios['solo_invisibles']}")
    con_contenido = vacios["pendientes"] - vacios["todo_vacio"] - vacios["sin_columnas"]
    print(f"     CON contenido real: {con_contenido}")
    print("     → si 'todo vacío' no domina, la hipótesis de F-O.4 se cae y la "
          "fase se rediseña.")

    # Muestra de pendientes CON contenido real: los vacíos ya están contados
    # arriba y no hay nada que mirar en ellos. Lo que importa es qué son los
    # que SÍ traen datos — si son ventas o gastos legibles, el problema no es
    # de filas en blanco sino de clasificación, y la fase cambia.
    muestra = await conn.fetch(
        "SELECT u.context_label, u.suggested_entity, u.row_data "
        "FROM unclassified_records u "
        "WHERE u.tenant_id=$1 AND u.status='PENDING' AND EXISTS ("
        "  SELECT 1 FROM jsonb_each_text(u.row_data) AS kv(k, v) "
        "  WHERE left(kv.k, 2) <> '__' "
        "    AND btrim(replace(coalesce(kv.v, ''), chr(160), ' ')) <> '') "
        "ORDER BY u.created_at DESC LIMIT 5",
        tid,
    )
    print("\n     muestra de pendientes CON contenido real (hasta 5):")
    for m in muestra:
        rd = _summary_dict(m["row_data"]) or {}
        visible = {k: v for k, v in rd.items() if not k.startswith("__")}
        print(f"\n     · motivo={m['context_label']!r} entity={m['suggested_entity']}")
        print(f"       row_data (sin claves __): "
              f"{json.dumps(visible, ensure_ascii=False)[:300]}")

    # ── UPLOADED FILES ───────────────────────────────────────────────────────
    p(f"UPLOADED FILES  (tenant {tid})")
    files = await conn.fetch(
        "SELECT id, original_filename, content_type, size_bytes, purpose, status, "
        "processing_status, rejection_reason, created_at, parsed_summary_json "
        "FROM uploaded_files WHERE tenant_id=$1 ORDER BY created_at DESC",
        tid,
    )
    print(f"  total archivos: {len(files)}")
    by_proc = Counter(f["processing_status"] for f in files)
    by_status = Counter(f["status"] for f in files)
    print(f"  processing_status: {dict(by_proc)}")
    print(f"  status: {dict(by_status)}")

    for f in files:
        print(f"\n  ── {f['original_filename']!r}  [{f['content_type']}]  {f['size_bytes']}B")
        print(f"     id={f['id']}  purpose={f['purpose']}  status={f['status']} "
              f"proc={f['processing_status']}  rej={f['rejection_reason']}  {f['created_at']}")
        s = _summary_dict(f["parsed_summary_json"])
        if s is None:
            print("     summary: NULL / no dict (no parseado o falló parseo)")
            continue
        # headers / columns crudas (en raíz o dentro de mapping_contexts)
        headers = s.get("headers") or s.get("columns")
        if headers is not None:
            print(f"     headers/columns: {json.dumps(headers, ensure_ascii=False)[:400]}")
        mc = s.get("mapping_contexts")
        if isinstance(mc, list) and mc:
            for i, ctx in enumerate(mc):
                if not isinstance(ctx, dict):
                    continue
                h = ctx.get("headers") or ctx.get("columns")
                label = ctx.get("context_label") or ctx.get("sheet") or ctx.get("group") or f"#{i}"
                if h is not None:
                    print(f"     mapping_context[{label}] headers: "
                          f"{json.dumps(h, ensure_ascii=False)[:300]}")
        if headers is None and not (isinstance(mc, list) and mc):
            # mostrar las keys disponibles para diagnóstico
            print(f"     (sin headers/columns; summary keys: {list(s.keys())})")


if __name__ == "__main__":
    asyncio.run(main())
