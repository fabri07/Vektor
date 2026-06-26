"""Read-only: ¿el tenant tiene el centinela "Local" y ventas que le apuntan?

Sirve para diagnosticar por qué "Local" no aparece en la sección Clientes:
el centinela solo se muestra si EXISTE (hay ventas sin cliente identificado).

Usage:
    DATABASE_URL='postgresql://...neon.tech/vektor?sslmode=require' \
        .venv/bin/python scripts/diag_local_customer.py fabriziosolafx@gmail.com

SOLO corre SELECT. No escribe nada. Nunca imprime la URL.
"""

import asyncio
import os
import sys

import asyncpg
from _db import normalize_dsn


def p(title: str) -> None:
    print(f"\n{'='*70}\n  {title}\n{'='*70}")


async def main(email: str) -> None:
    raw = os.environ.get("DATABASE_URL") or os.environ.get("DATABASE_URL_SYNC")
    if not raw:
        print("ERROR: exportá DATABASE_URL con la URL de Neon antes de correr.")
        sys.exit(2)
    dsn, sslarg = normalize_dsn(raw)
    conn = await asyncpg.connect(dsn, ssl=sslarg)
    try:
        await run(conn, email)
    finally:
        await conn.close()


async def run(conn: asyncpg.Connection, email: str) -> None:
    p(f"CLIENTES / CENTINELA 'Local'  ({email})")
    users = await conn.fetch(
        "SELECT user_id, tenant_id FROM users WHERE lower(email)=lower($1)", email
    )
    if not users:
        print("  ⚠ No existe ningún usuario con ese email.")
        like = await conn.fetch(
            "SELECT email FROM users WHERE email ILIKE $1 LIMIT 10",
            f"%{email.split('@')[0]}%",
        )
        if like:
            print("  Parecidos:", [r["email"] for r in like])
        return

    for tid in sorted({u["tenant_id"] for u in users}):
        print(f"\n  TENANT {tid}")

        sentinel = await conn.fetchrow(
            "SELECT id, name, deactivated_at, custom_fields->>'_sentinel' AS flag "
            "FROM customers "
            "WHERE tenant_id=$1 AND coalesce(custom_fields->>'_sentinel','')='true'",
            tid,
        )
        if sentinel is None:
            print(
                "  ⚠ NO existe el centinela 'Local' para este tenant.\n"
                "    → No hay ventas sin cliente identificado, así que NO debe aparecer\n"
                "      (la lista muestra 'Local' solo si existe; nunca lo inventa)."
            )
        else:
            sid = sentinel["id"]
            n_sales = await conn.fetchval(
                "SELECT count(*) FROM sales_entries "
                "WHERE tenant_id=$1 AND customer_id=$2 AND voided_at IS NULL",
                tid,
                sid,
            )
            print(
                f"  ✓ Centinela 'Local': id={sid}  name={sentinel['name']!r}  "
                f"flag={sentinel['flag']}  deactivated_at={sentinel['deactivated_at']}"
            )
            print(f"    ventas (no anuladas) que le apuntan: {n_sales}")
            if sentinel["deactivated_at"] is not None:
                print("    ⚠ está DESACTIVADO (deactivated_at != NULL) → no se lista.")

        total = await conn.fetchval(
            "SELECT count(*) FROM customers WHERE tenant_id=$1 AND deactivated_at IS NULL",
            tid,
        )
        reales = await conn.fetchval(
            "SELECT count(*) FROM customers WHERE tenant_id=$1 AND deactivated_at IS NULL "
            "AND coalesce(custom_fields->>'_sentinel','')<>'true'",
            tid,
        )
        nullc = await conn.fetchval(
            "SELECT count(*) FROM sales_entries "
            "WHERE tenant_id=$1 AND customer_id IS NULL AND voided_at IS NULL",
            tid,
        )
        print(
            f"  clientes activos: total={total}  reales={reales}  "
            f"centinela={total - reales}"
        )
        print(f"  ventas con customer_id NULL (legacy, sin rutear a 'Local'): {nullc}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: diag_local_customer.py <email>")
        sys.exit(2)
    asyncio.run(main(sys.argv[1]))
