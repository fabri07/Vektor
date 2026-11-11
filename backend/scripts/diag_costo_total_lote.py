"""Read-only: productos cuyo COSTO UNITARIO pudo entrar como TOTAL de la línea/lote.

CONTEXTO (pruebas del dueño, 2026-11-10)
----------------------------------------
Una columna «costo_total» se leía como ``unit_cost_ars`` (``column_mapping_service``,
regla ``"costo"``): el total del lote quedaba como costo de CADA unidad y el valor
de inventario (``Σ stock_units × unit_cost_ars``) se inflaba por la cantidad.
El fix corrige las sugerencias de acá en adelante; este script busca a quién le
pasó ANTES. **Solo detecta y reporta** — reparar es caso por caso, con el archivo
a la vista (un costo alto puede ser legítimo).

Dos señales, de más a menos fuerte:
1. ``confirmado``: el ``confirm`` de ``pipeline_events`` guarda el mapeo con el que
   se importó y una columna con «total» en el nombre apunta a ``unit_cost_ars``.
2. ``sospechoso``: producto activo con stock > 1 cuyo costo unitario supera a su
   precio de venta (un costo por encima del precio es raro y es el rastro típico).

Uso (la DATABASE_URL la provee el usuario desde su shell; NUNCA se imprime):
    DATABASE_URL='postgresql://...' .venv/bin/python scripts/diag_costo_total_lote.py <tenant_uuid>
    DATABASE_URL='postgresql://...' .venv/bin/python scripts/diag_costo_total_lote.py --all-tenants

``--all-tenants`` imprime UNA línea por tenant con hallazgos (confirmados o
sospechosos); los que no tienen ninguno no aparecen. Para el detalle de uno, el
modo con ``<tenant_uuid>``.

SOLO SELECT. Sin escrituras. Seguro contra producción.
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid

from _db import async_engine_config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

_SQL_CONFIRMADO = text(
    "SELECT file_id, created_at, detail->'mappings' AS mappings "
    "FROM pipeline_events "
    "WHERE tenant_id = :t AND stage = 'confirm' "
    "AND detail::text ~* '\"[^\"]*total[^\"]*\": *\"unit_cost_ars\"' "
    "ORDER BY created_at DESC LIMIT 50"
)
_SQL_SOSPECHOSOS = text(
    "SELECT id, name, stock_units, unit_cost_ars, sale_price_ars, "
    "stock_units * unit_cost_ars / NULLIF(base_units_per_sale_unit, 0) AS valor "
    "FROM products "
    "WHERE tenant_id = :t AND is_active AND stock_units > 1 "
    "AND unit_cost_ars IS NOT NULL AND unit_cost_ars > sale_price_ars "
    "ORDER BY valor DESC LIMIT 100"
)
_SQL_TOTAL = text(
    "SELECT COALESCE(SUM(stock_units * unit_cost_ars / "
    "NULLIF(base_units_per_sale_unit, 0)), 0) AS valor_inventario "
    "FROM products WHERE tenant_id = :t AND is_active AND unit_cost_ars IS NOT NULL"
)


def _p(titulo: str) -> None:
    print(f"\n{'=' * 70}\n  {titulo}\n{'=' * 70}")


async def main(tenant_id: uuid.UUID) -> None:
    url, connect_args = async_engine_config()
    engine = create_async_engine(url, connect_args=connect_args)
    try:
        async with engine.connect() as conn:
            _p("VALOR DE INVENTARIO ACTUAL (a costo)")
            total = (await conn.execute(_SQL_TOTAL, {"t": tenant_id})).scalar_one()
            print(f"  {total}")

            _p("CONFIRMADO: confirm con columna «total» → unit_cost_ars")
            filas = (await conn.execute(_SQL_CONFIRMADO, {"t": tenant_id})).all()
            if not filas:
                print("  (ninguno)")
            for f in filas:
                print(f"  file={f.file_id} creado={f.created_at}")
                print(f"    mappings={json.dumps(f.mappings, ensure_ascii=False)[:300]}")

            _p("SOSPECHOSO: costo unitario > precio de venta (stock > 1)")
            sosp = (await conn.execute(_SQL_SOSPECHOSOS, {"t": tenant_id})).all()
            if not sosp:
                print("  (ninguno)")
            for r in sosp:
                print(
                    f"  {r.id} | {r.name[:40]:40} | stock={r.stock_units} "
                    f"costo={r.unit_cost_ars} venta={r.sale_price_ars} valor={r.valor}"
                )
    finally:
        await engine.dispose()


_SQL_TENANTS = text("SELECT tenant_id FROM tenants ORDER BY created_at")
_SQL_RESUMEN = text(
    "SELECT "
    "(SELECT count(*) FROM pipeline_events WHERE tenant_id = :t AND stage = 'confirm' "
    " AND detail::text ~* '\"[^\"]*total[^\"]*\": *\"unit_cost_ars\"') AS confirmados, "
    "(SELECT count(*) FROM products WHERE tenant_id = :t AND is_active AND stock_units > 1 "
    " AND unit_cost_ars IS NOT NULL AND unit_cost_ars > sale_price_ars) AS sospechosos, "
    "(SELECT COALESCE(SUM(stock_units * unit_cost_ars / "
    " NULLIF(base_units_per_sale_unit, 0)), 0) FROM products "
    " WHERE tenant_id = :t AND is_active AND unit_cost_ars IS NOT NULL) AS valor_inventario"
)


async def resumen_de_todos() -> None:
    url, connect_args = async_engine_config()
    engine = create_async_engine(url, connect_args=connect_args)
    try:
        async with engine.connect() as conn:
            tenants = [r.tenant_id for r in (await conn.execute(_SQL_TENANTS)).all()]
            print(f"{len(tenants)} tenants revisados\n")
            con_hallazgos = 0
            for t in tenants:
                r = (await conn.execute(_SQL_RESUMEN, {"t": t})).one()
                if r.confirmados or r.sospechosos:
                    con_hallazgos += 1
                    print(
                        f"{t} | confirmados={r.confirmados} sospechosos={r.sospechosos} "
                        f"valor_inventario={r.valor_inventario}"
                    )
            print(f"\n{con_hallazgos} tenants con hallazgos")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    if sys.argv[1:] == ["--all-tenants"]:
        asyncio.run(resumen_de_todos())
    elif len(sys.argv) == 2:
        asyncio.run(main(uuid.UUID(sys.argv[1])))
    else:
        print(__doc__)
        sys.exit(2)
