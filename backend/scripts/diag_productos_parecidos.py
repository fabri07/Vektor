"""Read-only: grupos de productos activos con NOMBRE PARECIDO (sugerencia, no fusión).

Agrupa por palabras del artículo (sin plural, orden, tildes, unidades ni talle) con
`product_similarity.agrupar_parecidos`. Por grupo imprime cada miembro con su stock,
costo y talles: un grupo con `mismos_numeros=False` (T40 vs T42) probablemente NO son
el mismo producto. NO fusiona, no crea alias, no escribe.

Uso (la DATABASE_URL la provee el usuario desde su shell; NUNCA se imprime):
    DATABASE_URL='postgresql://...' \
        .venv/bin/python scripts/diag_productos_parecidos.py <tenant_uuid>

SOLO SELECT. Seguro contra producción.
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _db import async_engine_config  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

from app.application.services.product_similarity import (  # noqa: E402
    ProductoParaComparar,
    agrupar_parecidos,
)

_SQL = text(
    "SELECT id, name, stock_units, unit_cost_ars, sale_price_ars FROM products "
    "WHERE tenant_id = :t AND is_active"
)


async def main(tenant_id: uuid.UUID) -> None:
    url, connect_args = async_engine_config()
    engine = create_async_engine(url, connect_args=connect_args)
    try:
        async with engine.connect() as conn:
            filas = (await conn.execute(_SQL, {"t": tenant_id})).all()
    finally:
        await engine.dispose()

    por_id = {f.id: f for f in filas}
    grupos = agrupar_parecidos([ProductoParaComparar(f.id, f.name) for f in filas])
    print(f"{len(filas)} productos activos, {len(grupos)} grupos de nombre parecido\n")
    for g in grupos:
        marca = "mismos talles" if g.mismos_numeros else "⚠ TALLES DISTINTOS (¿son distintos?)"
        print(f"== {' '.join(g.clave)}  [{marca}]")
        for m in g.miembros:
            f = por_id[m.id]
            print(
                f"   {m.id} | {m.name[:40]:40} | talles={sorted(m.numeros)} "
                f"stock={f.stock_units} costo={f.unit_cost_ars} venta={f.sale_price_ars}"
            )
        print()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(2)
    asyncio.run(main(uuid.UUID(sys.argv[1])))
