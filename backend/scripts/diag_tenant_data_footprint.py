"""Read-only: de dónde salió cada dato que hoy tiene un tenant.

Responde dos preguntas antes de tocar nada:

1. **¿La reversión de archivos borrados dejó residuos?** — datos VIVOS cuyo
   ``source_upload_id`` apunta a un archivo que ya fue borrado. Si aparecen, el
   borrado no limpió (o el archivo se borró antes del commit del
   borrado-que-revierte).
2. **¿Qué queda si se borran TODOS los archivos?** — el remanente sin origen de
   archivo: cargas manuales, chat, seed. Eso es lo que un reset de cuenta
   borraría y el borrado por archivo nunca puede tocar.

Sirve para decidir con números si alcanza con purgar/borrar por archivo o si hace
falta un reset del tenant.

PRODUCTOS: no tienen columna de origen. Se separan en tres grupos:
  - ``con_ledger``    — un ``DataRepairRun`` de tipo ``INGESTION_IMPORT`` los ata
                        a un archivo concreto. El borrado SÍ puede revertirlos.
  - ``solo_movimiento`` — no hay ledger, pero tienen un ``inventory_movement``
                        con ``source_upload_id``. Sugiere que vinieron de un
                        archivo; NO alcanza para afirmar que ese archivo los CREÓ.
  - ``sin_rastro``    — ni ledger ni movimiento de archivo: alta manual/chat, o
                        import tan viejo que no dejó ninguna huella.

SOLO SELECT. No escribe nada. NUNCA imprime la connection URL.

Usage:
    DATABASE_URL='postgresql://...neon.tech/...' \
        .venv/bin/python scripts/diag_tenant_data_footprint.py --email agustinalahora4@gmail.com

    ... scripts/diag_tenant_data_footprint.py --tenant <uuid>
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

import asyncpg

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _db import normalize_dsn  # noqa: E402


def _titulo(texto: str) -> None:
    print(f"\n{'=' * 72}\n  {texto}\n{'=' * 72}")


async def _resolver_tenant(
    conn: asyncpg.Connection, email: str | None, tenant: str | None
) -> asyncpg.Record | None:
    # `tenants` NO tiene business_name/business_type: son `display_name`/
    # `legal_name` + `status`, y el rubro vive en `business_profiles.vertical_code`.
    _select = (
        "SELECT t.tenant_id, t.display_name, t.legal_name, t.status, t.is_demo, "
        "       t.created_at, bp.vertical_code "
        "FROM tenants t "
        "LEFT JOIN business_profiles bp ON bp.tenant_id = t.tenant_id "
    )
    if tenant:
        return await conn.fetchrow(_select + "WHERE t.tenant_id = $1::uuid", tenant)
    return await conn.fetchrow(
        _select
        + "JOIN users u ON u.tenant_id = t.tenant_id "
        "WHERE lower(u.email) = lower($1) LIMIT 1",
        email,
    )


async def _archivos(conn: asyncpg.Connection, tid: str) -> None:
    _titulo("ARCHIVOS")
    filas = await conn.fetch(
        "SELECT id::text AS id, original_filename, processing_status, "
        "       deleted_at, ingestion_version, created_at "
        "FROM uploaded_files WHERE tenant_id = $1::uuid ORDER BY created_at",
        tid,
    )
    if not filas:
        print("  (sin archivos)")
        return
    vivos = [f for f in filas if f["deleted_at"] is None]
    borrados = [f for f in filas if f["deleted_at"] is not None]
    print(f"  total: {len(filas)}   vivos: {len(vivos)}   borrados: {len(borrados)}")
    print()
    for f in filas:
        estado = "BORRADO" if f["deleted_at"] else f["processing_status"]
        # ingestion_version >= 3 es el que dejó ledger de reversa de productos.
        ver = f["ingestion_version"]
        ledger = "con-ledger" if (ver or 0) >= 3 else "SIN-ledger"
        print(
            f"  [{estado:>18}] {ledger:>10}  "
            f"{str(f['created_at'])[:10]}  {f['original_filename'][:44]}"
        )


async def _datos_por_origen(conn: asyncpg.Connection, tid: str) -> None:
    _titulo("DATOS VIVOS, POR ORIGEN")
    # Una fila por tabla: cuántos vienen de archivo vivo / archivo BORRADO
    # (= residuo) / sin archivo (manual, chat, seed).
    consultas = [
        ("ventas", "sales_entries", "source_upload_id", "voided_at IS NULL"),
        ("gastos", "expense_entries", "source_upload_id", "voided_at IS NULL"),
        ("movim. stock", "inventory_movements", "source_upload_id", "voided_at IS NULL"),
    ]
    print(f"  {'tabla':<14} {'total':>7} {'de arch. vivo':>14} {'RESIDUO':>9} {'sin arch.':>10}")
    for etiqueta, tabla, col, vivo_filtro in consultas:
        row = await conn.fetchrow(
            f"""
            SELECT count(*) AS total,
                   count(*) FILTER (
                       WHERE t.{col} IS NOT NULL AND f.deleted_at IS NULL
                   ) AS de_vivo,
                   count(*) FILTER (
                       WHERE t.{col} IS NOT NULL AND f.deleted_at IS NOT NULL
                   ) AS residuo,
                   count(*) FILTER (WHERE t.{col} IS NULL) AS sin_archivo
            FROM {tabla} t
            LEFT JOIN uploaded_files f ON f.id = t.{col}
            WHERE t.tenant_id = $1::uuid AND {vivo_filtro}
            """,  # noqa: S608 — tabla/columna son literales de este módulo
            tid,
        )
        marca = "  <-- residuo" if (row["residuo"] or 0) > 0 else ""
        print(
            f"  {etiqueta:<14} {row['total']:>7} {row['de_vivo']:>14} "
            f"{row['residuo']:>9} {row['sin_archivo']:>10}{marca}"
        )

    otros = await conn.fetchrow(
        """
        SELECT count(*) AS total,
               count(*) FILTER (WHERE f.deleted_at IS NOT NULL) AS residuo
        FROM unclassified_records u
        LEFT JOIN uploaded_files f ON f.id = u.uploaded_file_id
        WHERE u.tenant_id = $1::uuid
        """,
        tid,
    )
    print(f"  {'otros':<14} {otros['total']:>7} {'':>14} {otros['residuo']:>9}")


async def _productos(conn: asyncpg.Connection, tid: str) -> None:
    _titulo("PRODUCTOS — ¿se pueden atribuir a un archivo?")
    total = await conn.fetchval(
        "SELECT count(*) FROM products WHERE tenant_id = $1::uuid AND is_active IS TRUE",
        tid,
    )
    con_ledger = await conn.fetchval(
        """
        SELECT count(DISTINCT i.product_id)
        FROM data_repair_items i
        JOIN data_repair_runs r ON r.id = i.run_id
        WHERE r.tenant_id = $1::uuid AND r.repair_type = 'INGESTION_IMPORT'
          AND i.product_id IS NOT NULL
        """,
        tid,
    )
    solo_mov = await conn.fetchval(
        """
        SELECT count(DISTINCT m.product_id)
        FROM inventory_movements m
        WHERE m.tenant_id = $1::uuid AND m.source_upload_id IS NOT NULL
          AND m.product_id IS NOT NULL
        """,
        tid,
    )
    print(f"  activos                       : {total}")
    print(f"  atribuibles por ledger        : {con_ledger}   (el borrado SÍ los revierte)")
    print(f"  con movimiento de archivo     : {solo_mov}   (sugiere origen, no lo prueba)")
    print(f"  sin rastro de archivo         : {max(0, (total or 0) - (solo_mov or 0))}")


async def _resto(conn: asyncpg.Connection, tid: str) -> None:
    _titulo("RESTO DE LA CUENTA")
    for etiqueta, sql in (
        ("clientes", "SELECT count(*) FROM customers WHERE tenant_id=$1::uuid"),
        ("proveedores", "SELECT count(*) FROM suppliers WHERE tenant_id=$1::uuid"),
        (
            "snapshots de salud",
            "SELECT count(*) FROM health_score_snapshots WHERE tenant_id=$1::uuid",
        ),
        (
            "huellas anti-duplicado",
            "SELECT count(*) FROM operation_fingerprints WHERE tenant_id=$1::uuid",
        ),
        ("cierres de caja", "SELECT count(*) FROM cash_closes WHERE tenant_id=$1::uuid"),
        ("usuarios", "SELECT count(*) FROM users WHERE tenant_id=$1::uuid"),
    ):
        try:
            valor = await conn.fetchval(sql, tid)
        except asyncpg.PostgresError as exc:  # tabla ausente en un entorno viejo
            print(f"  {etiqueta:<26}: (no se pudo leer: {type(exc).__name__})")
            continue
        print(f"  {etiqueta:<26}: {valor}")


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    grupo = parser.add_mutually_exclusive_group(required=True)
    grupo.add_argument("--email", help="email de un usuario del tenant")
    grupo.add_argument("--tenant", help="tenant_id")
    args = parser.parse_args()

    raw = os.environ.get("DATABASE_URL") or os.environ.get("DATABASE_URL_SYNC")
    if not raw:
        print("ERROR: exportá DATABASE_URL antes de correr.")
        return 2
    dsn, sslarg = normalize_dsn(raw)
    conn = await asyncpg.connect(dsn, ssl=sslarg)
    try:
        tenant = await _resolver_tenant(conn, args.email, args.tenant)
        if tenant is None:
            print("No se encontró el tenant.")
            return 1
        _titulo("CUENTA")
        print(f"  tenant_id : {tenant['tenant_id']}")
        print(f"  negocio   : {tenant['display_name']} ({tenant['legal_name']})")
        print(f"  rubro     : {tenant['vertical_code']}")
        print(f"  status    : {tenant['status']}   is_demo: {tenant['is_demo']}")
        print(f"  creada    : {str(tenant['created_at'])[:19]}")

        tid = str(tenant["tenant_id"])
        await _archivos(conn, tid)
        await _datos_por_origen(conn, tid)
        await _productos(conn, tid)
        await _resto(conn, tid)

        print()
        print("Lectura: 'RESIDUO' = dato vivo cuyo archivo ya fue borrado — el borrado")
        print("no lo limpió. 'sin arch.' = carga manual/chat/seed: ningún borrado por")
        print("archivo lo puede tocar, solo un reset de cuenta.")
    finally:
        await conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
