"""Read-only: reconcilia 'mercadería en gastos' vs catálogo de productos.

Responde: ¿cuántas compras (COGS) hay, cuántos productos DISTINTOS se compraron,
cuántos productos hay en el catálogo, y se crearon productos nuevos o se mantuvo
el catálogo? SOLO SELECT. Nunca imprime la URL.

Usage:
    DATABASE_URL=... .venv/bin/python scripts/diag_mercaderias.py fabriziosolafx@gmail.com
"""

from __future__ import annotations

import asyncio
import os
import sys

import asyncpg
from _db import normalize_dsn


def p(t: str) -> None:
    print(f"\n{'='*70}\n  {t}\n{'='*70}")


async def main(email: str) -> None:
    raw = os.environ.get("DATABASE_URL") or os.environ.get("DATABASE_URL_SYNC")
    if not raw:
        print("ERROR: exportá DATABASE_URL.")
        sys.exit(2)
    dsn, sslarg = normalize_dsn(raw)
    conn = await asyncpg.connect(dsn, ssl=sslarg)
    try:
        await run(conn, email)
    finally:
        await conn.close()


async def run(conn: asyncpg.Connection, email: str) -> None:
    u = await conn.fetchrow(
        "SELECT tenant_id FROM users WHERE lower(email)=lower($1) LIMIT 1", email
    )
    if not u:
        print("  ⚠ usuario inexistente.")
        return
    tid = u["tenant_id"]

    p("GASTOS DE MERCADERÍA (COGS/INVENTORY)")
    g = await conn.fetchrow(
        "SELECT count(*) total, count(product_id) con_prod, "
        "count(*)-count(product_id) sin_prod, count(DISTINCT product_id) distintos "
        "FROM expense_entries WHERE tenant_id=$1 AND voided_at IS NULL "
        "AND (category='INVENTORY' OR expense_type='COGS')",
        tid,
    )
    print(f"  líneas de compra (entradas COGS) = {g['total']}")
    print(f"  con producto vinculado           = {g['con_prod']}")
    print(f"  SIN producto vinculado           = {g['sin_prod']}  (no salen en vista por-producto)")
    print(f"  PRODUCTOS DISTINTOS comprados    = {g['distintos']}")

    p("CATÁLOGO DE PRODUCTOS")
    pr = await conn.fetchrow(
        "SELECT count(*) total, "
        "count(*) FILTER (WHERE provenance='REAL') real, "
        "min(created_at) primero, max(created_at) ultimo "
        "FROM products WHERE tenant_id=$1 AND deactivated_at IS NULL",
        tid,
    )
    print(f"  productos en catálogo = {pr['total']}  (REAL={pr['real']})")
    print(f"  creados entre {pr['primero']} y {pr['ultimo']}")
    nuevos = await conn.fetchrow(
        "SELECT count(*) n FROM products WHERE tenant_id=$1 AND deactivated_at IS NULL "
        "AND created_at >= '2026-06-13'",
        tid,
    )
    print(f"  productos creados el 13/06 o después (¿nuevos por el import?) = {nuevos['n']}")

    p("¿COINCIDEN? — productos comprados vs catálogo")
    sin_compra = await conn.fetch(
        "SELECT p.sku, p.name, p.stock_units FROM products p "
        "WHERE p.tenant_id=$1 AND p.deactivated_at IS NULL AND NOT EXISTS ("
        "  SELECT 1 FROM expense_entries e WHERE e.tenant_id=$1 AND e.product_id=p.id "
        "  AND e.voided_at IS NULL AND (e.category='INVENTORY' OR e.expense_type='COGS')) "
        "ORDER BY p.name",
        tid,
    )
    print(f"  productos del catálogo SIN ninguna compra registrada = {len(sin_compra)}")
    for r in sin_compra[:20]:
        print(f"    {str(r['sku'])[:10]:<10} {str(r['name'])[:35]:<35} stock={r['stock_units']}")
    print("\n  → 'mercadería en gastos' cuenta COMPRAS (líneas), no productos. La vista")
    print("    por-producto muestra solo los productos DISTINTOS con compra vinculada.")
    print("    No tiene por qué igualar el catálogo: un producto puede existir sin compra")
    print("    registrada (servicios/virtuales, o alta sin compra). Productos nuevos por")
    print("    el import = el número de arriba (debería ser 0 si fue reposición).")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: DATABASE_URL=... python scripts/diag_mercaderias.py <email>")
        sys.exit(2)
    asyncio.run(main(sys.argv[1]))
