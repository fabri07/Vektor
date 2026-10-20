"""Read-only: gastos de mercadería que no dicen qué artículos son (B·5b, 2026-10-20).

Decisión del dueño: una compra de mercadería sin los artículos que la componen
no es un dato útil. Desde B·5b no entra ninguna nueva —la carga manual, el chat,
«Otros», el Libro Diario y las hojas de compras la mandan a resolver por
líneas—, pero las que ya estaban cargadas siguen ahí. Este script las cuenta por
negocio: gastos vivos INVENTORY/COGS sin ``product_id``, por origen (archivo o
a mano), con el total y unos ejemplos.

SOLO SELECT. No repara nada: qué hacer con ellos (mandarlos a «Otros» para
resolverlos, o dejarlos) es una decisión aparte del dueño.
Nunca imprime la connection URL. Corre desde ``backend/``.

Usage:
    DATABASE_URL=... .venv/bin/python scripts/diag_compras_sin_articulos.py
    DATABASE_URL=... .venv/bin/python scripts/diag_compras_sin_articulos.py dueño@mail.com
"""

from __future__ import annotations

import asyncio
import os
import sys
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncpg  # noqa: E402
from _db import normalize_dsn  # noqa: E402

_EJEMPLOS = 5

_SQL = """
SELECT e.tenant_id,
       t.display_name,
       (e.source_upload_id IS NOT NULL) AS de_archivo,
       count(*)                          AS cantidad,
       coalesce(sum(e.amount), 0)        AS total,
       min(e.transaction_date)::date     AS desde,
       max(e.transaction_date)::date     AS hasta
  FROM expense_entries e
  JOIN tenants t ON t.tenant_id = e.tenant_id
 WHERE e.voided_at IS NULL
   AND e.product_id IS NULL
   AND (e.category = 'INVENTORY' OR e.expense_type = 'COGS')
   {filtro}
 GROUP BY e.tenant_id, t.display_name, de_archivo
 ORDER BY total DESC
"""

_SQL_EJEMPLOS = """
SELECT e.transaction_date::date AS fecha, e.amount, e.description, e.payment_method
  FROM expense_entries e
 WHERE e.voided_at IS NULL
   AND e.product_id IS NULL
   AND (e.category = 'INVENTORY' OR e.expense_type = 'COGS')
   AND e.tenant_id = $1
 ORDER BY e.amount DESC
 LIMIT $2
"""


async def main(email: str | None) -> None:
    raw = os.environ.get("DATABASE_URL") or os.environ.get("DATABASE_URL_SYNC")
    if not raw:
        print("ERROR: exportá DATABASE_URL.")
        sys.exit(2)
    dsn, sslarg = normalize_dsn(raw)
    conn = await asyncpg.connect(dsn, ssl=sslarg)
    try:
        filtro, args = "", []
        if email:
            u = await conn.fetchrow(
                "SELECT tenant_id FROM users WHERE lower(email)=lower($1) LIMIT 1", email
            )
            if not u:
                print(f"No hay un usuario con el email {email}.")
                return
            filtro, args = "AND e.tenant_id = $1", [u["tenant_id"]]
        filas = await conn.fetch(_SQL.format(filtro=filtro), *args)
        if not filas:
            print("No hay gastos de mercadería sin artículos. Nada que resolver.")
            return
        vistos: set[object] = set()
        total = Decimal("0")
        for f in filas:
            origen = "de archivo" if f["de_archivo"] else "a mano / chat"
            total += Decimal(f["total"])
            print(
                f"{f['display_name']} — {origen}: {f['cantidad']} gasto(s), "
                f"${Decimal(f['total']):,.2f} ({f['desde']} a {f['hasta']})"
            )
            if f["tenant_id"] not in vistos:
                vistos.add(f["tenant_id"])
                for e in await conn.fetch(_SQL_EJEMPLOS, f["tenant_id"], _EJEMPLOS):
                    print(
                        f"    {e['fecha']}  ${Decimal(e['amount']):,.2f}  "
                        f"{(e['description'] or '').strip()[:60]}  ({e['payment_method']})"
                    )
        print(f"\nTotal: ${total:,.2f} en {len(vistos)} negocio(s).")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else None))
