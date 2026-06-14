"""Read-only: detecta gastos duplicados (mismo fecha+monto, descripciones que
difieren solo por sufijo '— proveedor'). SOLO SELECT. Nunca imprime la URL.

Usage:
    DATABASE_URL=... .venv/bin/python scripts/diag_dup_expenses.py fabriziosolafx@gmail.com
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections import defaultdict

import asyncpg
from _db import normalize_dsn


def p(t: str) -> None:
    print(f"\n{'='*70}\n  {t}\n{'='*70}")


def _base(desc: str) -> str:
    """Descripción sin el sufijo '— proveedor' ni espacios, para agrupar variantes."""
    d = (desc or "").split(" — ")[0].strip().lower()
    return d


async def main(email: str) -> None:
    raw = os.environ.get("DATABASE_URL") or os.environ.get("DATABASE_URL_SYNC")
    if not raw:
        print("ERROR: exportá DATABASE_URL.")
        sys.exit(2)
    dsn, sslarg = normalize_dsn(raw)
    conn = await asyncpg.connect(dsn, ssl=sslarg)
    try:
        u = await conn.fetchrow(
            "SELECT tenant_id FROM users WHERE lower(email)=lower($1) LIMIT 1", email
        )
        if not u:
            print("  ⚠ usuario inexistente.")
            return
        tid = u["tenant_id"]

        rows = await conn.fetch(
            "SELECT id, date(transaction_date) d, amount, description, category, "
            "expense_type, product_id, "
            "custom_fields->>'_fix_batch' AS fix_batch "
            "FROM expense_entries WHERE tenant_id=$1 AND voided_at IS NULL "
            "ORDER BY transaction_date, amount",
            tid,
        )
        p(f"GASTOS no anulados: {len(rows)}")

        # Grupos por (fecha, monto, descripción-base) con >1 fila = candidato a duplicado
        groups: dict[tuple, list] = defaultdict(list)
        for r in rows:
            groups[(str(r["d"]), f"{float(r['amount']):.2f}", _base(r["description"]))].append(r)

        dups = {k: v for k, v in groups.items() if len(v) > 1}
        total_dup_rows = sum(len(v) for v in dups.values())
        total_extra = sum(len(v) - 1 for v in dups.values())
        extra_monto = sum(float(v[0]["amount"]) * (len(v) - 1) for v in dups.values())

        p("DUPLICADOS (mismo fecha+monto+descripción-base, >1 fila)")
        print(f"  grupos duplicados = {len(dups)}")
        print(f"  filas involucradas = {total_dup_rows}  |  filas EXTRA (a anular) = {total_extra}")
        print(f"  $ duplicado (sobre-conteo COGS/gastos) ≈ {extra_monto:,.2f}")
        print("\n  ejemplos (hasta 25 grupos):")
        for (d, amt, base), v in list(dups.items())[:25]:
            print(f"   {d}  ${amt}  «{base[:40]}»  ×{len(v)}")
            for r in v:
                print(f"      id={str(r['id'])[:8]} desc={r['description'][:45]!r} "
                      f"cat={r['category']}/{r['expense_type']} "
                      f"prod={'sí' if r['product_id'] else 'no'} batch={r['fix_batch']}")
    finally:
        await conn.close()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: DATABASE_URL=... python scripts/diag_dup_expenses.py <email>")
        sys.exit(2)
    asyncio.run(main(sys.argv[1]))
