"""Read-only cash diagnostic — por qué el cash_score da 0.

Usage:
    DATABASE_URL='postgresql://...neon.tech/vektor?sslmode=require' \
        .venv/bin/python scripts/diag_cash.py fabriziosolafx@gmail.com

ONLY runs SELECT statements. No writes. Nunca imprime la connection URL.
Reporta las tres entradas reales del cash_score:
  1. profile.cash_on_hand_estimate_ars + onboarding_recent (gate de 7 días)
  2. cash_closes (arqueo) — última counted_total_ars si existe
  3. movimientos en efectivo (sales/expenses payment_method='cash', no anulados)
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
    p(f"USUARIO / TENANT  ({email})")
    users = await conn.fetch(
        "SELECT user_id, tenant_id, email FROM users WHERE lower(email) = lower($1)",
        email,
    )
    if not users:
        print("  ⚠ No existe usuario con ese email.")
        return
    tenant_ids = list({u["tenant_id"] for u in users})
    for u in users:
        print(f"  user_id={u['user_id']}  tenant_id={u['tenant_id']}")

    for tid in tenant_ids:
        p(f"1) PROFILE (estimado onboarding + gate 7 días)  tenant={tid}")
        prof = await conn.fetchrow(
            "SELECT vertical_code, onboarding_completed, updated_at, "
            "cash_on_hand_estimate_ars, monthly_fixed_expenses_estimate_ars "
            "FROM business_profiles WHERE tenant_id = $1",
            tid,
        )
        if prof is None:
            print("  ⚠ Sin business_profile.")
        else:
            print(f"  vertical={prof['vertical_code']}")
            print(f"  onboarding_completed={prof['onboarding_completed']}")
            print(f"  updated_at={prof['updated_at']}  (onboarding_recent = "
                  "completed AND updated_at >= now-7d)")
            print(f"  cash_on_hand_estimate_ars={prof['cash_on_hand_estimate_ars']}")
            print(f"  monthly_fixed_expenses_estimate_ars="
                  f"{prof['monthly_fixed_expenses_estimate_ars']}")
            rec = await conn.fetchrow(
                "SELECT (updated_at >= now() - interval '7 days') AS recent FROM "
                "business_profiles WHERE tenant_id = $1",
                tid,
            )
            print(f"  → updated_at dentro de 7 días: {rec['recent'] if rec else '?'}")

        p(f"2) CASH_CLOSES (arqueo)  tenant={tid}")
        cc = await conn.fetch(
            "SELECT close_date, expected_total_ars, counted_total_ars, opening_float_ars "
            "FROM cash_closes WHERE tenant_id = $1 ORDER BY close_date DESC LIMIT 5",
            tid,
        )
        if not cc:
            print("  (sin arqueos — no hay CashClose para este tenant)")
        else:
            for r in cc:
                print(f"  {r['close_date']}  contado={r['counted_total_ars']}  "
                      f"esperado={r['expected_total_ars']}  fondo={r['opening_float_ars']}")

        p(f"3) MOVIMIENTOS EN EFECTIVO (payment_method='cash', no anulados)  tenant={tid}")
        sales = await conn.fetch(
            "SELECT payment_method, count(*) AS n, coalesce(sum(amount),0) AS total "
            "FROM sales_entries WHERE tenant_id = $1 AND voided_at IS NULL "
            "GROUP BY payment_method ORDER BY total DESC",
            tid,
        )
        print("  VENTAS por método:")
        for r in sales:
            print(f"    {r['payment_method']:<16} n={r['n']:<6} total={r['total']}")
        exp = await conn.fetch(
            "SELECT payment_method, count(*) AS n, coalesce(sum(amount),0) AS total "
            "FROM expense_entries WHERE tenant_id = $1 AND voided_at IS NULL "
            "GROUP BY payment_method ORDER BY total DESC",
            tid,
        )
        print("  GASTOS por método:")
        for r in exp:
            print(f"    {r['payment_method']:<16} n={r['n']:<6} total={r['total']}")

        # Neto LÍQUIDO = cash + transfer + qr + debit_card (disponible hoy).
        # Excluye 'account' (fiado), 'credit_card' (acreditación ~30d), 'other'.
        liq = "('cash','transfer','qr','debit_card')"
        net = await conn.fetchrow(
            "SELECT "
            f"(SELECT coalesce(sum(amount),0) FROM sales_entries "
            f"  WHERE tenant_id=$1 AND voided_at IS NULL AND payment_method IN {liq}) AS liq_in, "
            f"(SELECT coalesce(sum(amount),0) FROM expense_entries "
            f"  WHERE tenant_id=$1 AND voided_at IS NULL AND payment_method IN {liq}) AS liq_out, "
            "(SELECT coalesce(sum(amount),0) FROM sales_entries "
            "  WHERE tenant_id=$1 AND voided_at IS NULL AND payment_method='cash') AS cash_in, "
            "(SELECT coalesce(sum(amount),0) FROM expense_entries "
            "  WHERE tenant_id=$1 AND voided_at IS NULL AND payment_method='cash') AS cash_out",
            tid,
        )
        liq_in, liq_out = net["liq_in"], net["liq_out"]
        cash_in, cash_out = net["cash_in"], net["cash_out"]
        print(f"\n  NETO LÍQUIDO all-time (cash+transfer+qr+debit) = {liq_in} − {liq_out} = "
              f"{liq_in - liq_out}   ← propuesta de cash_on_hand_est (tier 2)")
        print(f"  NETO solo efectivo all-time                    = {cash_in} − {cash_out} = "
              f"{cash_in - cash_out}")
        allnet = await conn.fetchrow(
            "SELECT "
            "(SELECT coalesce(sum(amount),0) FROM sales_entries "
            "  WHERE tenant_id=$1 AND voided_at IS NULL) AS s, "
            "(SELECT coalesce(sum(amount),0) FROM expense_entries "
            "  WHERE tenant_id=$1 AND voided_at IS NULL) AS e",
            tid,
        )
        print(f"  NETO TODOS los métodos all-time          = {allnet['s']} − {allnet['e']} = "
              f"{allnet['s'] - allnet['e']}")
        print("\n  → cash_on_hand_est actual del código = "
              "(estimate si onboarding_recent, si no 0). Ver sección 1.")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: DATABASE_URL=... python scripts/diag_cash.py <email>")
        sys.exit(2)
    asyncio.run(main(sys.argv[1]))
