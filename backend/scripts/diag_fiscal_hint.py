"""Read-only: infiere condición fiscal desde los gastos (pagos a AFIP/ARCA).

Heurística pedida: si el negocio paga impuestos al ente regulador (AFIP/ARCA,
monotributo, IVA, ingresos brutos, etc.) → está registrado. Si no aparece nada
→ probablemente informal. SOLO informativo, no decide nada por sí solo.

SOLO SELECT. Nunca imprime la URL.

Usage:
    DATABASE_URL='...' .venv/bin/python scripts/diag_fiscal_hint.py fabriziosolafx@gmail.com
"""

from __future__ import annotations

import asyncio
import os
import sys

import asyncpg
from _db import normalize_dsn

# Palabras que delatan un pago al fisco / régimen tributario.
TAX_KEYWORDS = [
    "afip", "arca", "monotributo", "monotributista", "iva", "ingresos brutos",
    "iibb", "rentas", "agip", "arba", "impuesto", "fiscal", "tributar",
    "tributo", "f931", "f. 931", "autonomo", "autónomo", "ddjj", "afip/arca",
    "tasa municipal", "habilitacion", "habilitación",
]


def p(title: str) -> None:
    print(f"\n{'='*70}\n  {title}\n{'='*70}")


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

    prof = await conn.fetchrow(
        "SELECT fiscal_condition FROM business_profiles WHERE tenant_id=$1", tid
    )
    p("CONDICIÓN FISCAL ACTUAL EN EL PERFIL")
    print(f"  fiscal_condition = {prof['fiscal_condition'] if prof else '(sin profile)'!r}  "
          "(NULL = no configurado)")

    # Construir un ILIKE OR sobre description + category + custom_fields::text
    like_clauses = " OR ".join(
        f"(lower(description) LIKE '%{k}%' OR lower(coalesce(category,'')) LIKE '%{k}%' "
        f"OR lower(coalesce(custom_fields::text,'')) LIKE '%{k}%')"
        for k in TAX_KEYWORDS
    )
    p("GASTOS QUE PARECEN PAGOS AL FISCO (AFIP/ARCA/impuestos)")
    rows = await conn.fetch(
        "SELECT date(transaction_date) d, amount, category, expense_type, "
        "payment_method, description "
        f"FROM expense_entries WHERE tenant_id=$1 AND voided_at IS NULL AND ({like_clauses}) "  # noqa: S608
        "ORDER BY transaction_date DESC LIMIT 50",
        tid,
    )
    if not rows:
        print("  (NINGÚN gasto matchea keywords fiscales)")
        print("  → señal de NEGOCIO INFORMAL (no se ven pagos de impuestos).")
    else:
        print(f"  {len(rows)} gasto(s) con pinta fiscal:")
        for r in rows:
            print(f"    {r['d']}  ${r['amount']:<12} [{r['category']}/{r['expense_type']}] "
                  f"{r['payment_method']:<10} {(r['description'] or '')[:55]!r}")
        # ¿pinta de monotributo (cuota fija mensual) o RI (IVA/F931 variable)?
        has_iva = any("iva" in (r["description"] or "").lower() for r in rows)
        has_mono = any("monotrib" in (r["description"] or "").lower() for r in rows)
        print()
        print(f"  menciones de 'monotributo' = {has_mono}   menciones de 'IVA' = {has_iva}")
        if has_mono and not has_iva:
            print("  → pinta MONOTRIBUTO (cuota fija; IVA incluido en el costo).")
        elif has_iva:
            print("  → pinta RESPONSABLE INSCRIPTO (discrimina IVA).")
        else:
            print("  → registrado, pero el sub-régimen no es obvio desde los datos.")

    p("DISTRIBUCIÓN DE CATEGORÍAS OPEX (contexto)")
    cats = await conn.fetch(
        "SELECT category, count(*) n, coalesce(sum(amount),0) total FROM expense_entries "
        "WHERE tenant_id=$1 AND voided_at IS NULL AND expense_type='OPEX' "
        "GROUP BY category ORDER BY total DESC LIMIT 20",
        tid,
    )
    for r in cats:
        print(f"  {str(r['category']):<22} n={r['n']:<5} total={r['total']}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: DATABASE_URL=... python scripts/diag_fiscal_hint.py <email>")
        sys.exit(2)
    asyncio.run(main(sys.argv[1]))
