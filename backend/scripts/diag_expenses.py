"""Read-only expense-import diagnostic for a real account.

Usage:
    DATABASE_URL='postgresql://...neon.tech/vektor?sslmode=require' \
        .venv/bin/python scripts/diag_expenses.py usuario@ejemplo.com

ONLY runs SELECT statements. No writes. Safe against production.

Answers, per tenant of the given user:
  1. Did each uploaded file end DONE or NEEDS_CONFIRMATION, and what did it import?
  2. Which expense_entries exist: by category, date range, payment method, provenance.
  3. Which IMPORT_TABULAR_FILE pending actions ran and with which confirmed_fields.
"""

import asyncio
import json
import os
import sys

import asyncpg
from diag_account import normalize_dsn, p


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
    users = await conn.fetch(
        "SELECT user_id, tenant_id, email FROM users WHERE lower(email) = lower($1)", email
    )
    if not users:
        print(f"⚠ No existe usuario con email {email!r}.")
        return
    tenant_ids = list({u["tenant_id"] for u in users})

    for tid in tenant_ids:
        # ── 1. ARCHIVOS: estado final + qué se importó ────────────────────
        p(f"ARCHIVOS SUBIDOS  (tenant {tid})")
        files = await conn.fetch(
            "SELECT id, original_filename, processing_status, created_at, parsed_summary_json "
            "FROM uploaded_files WHERE tenant_id=$1 AND deleted_at IS NULL "
            "ORDER BY created_at DESC",
            tid,
        )
        if not files:
            print("  (sin archivos)")
        for f in files:
            summ = f["parsed_summary_json"]
            s = {}
            if summ:
                try:
                    s = json.loads(summ) if isinstance(summ, str) else summ
                except (TypeError, ValueError):
                    s = {}
            if not isinstance(s, dict):
                s = {}
            buckets = {
                k: len(s[k])
                for k in ("ventas_detectadas", "gastos_detectados", "stock_detectado")
                if isinstance(s.get(k), list)
            }
            print(f"\n  ── {f['original_filename']!r}  proc={f['processing_status']}"
                  f"  {f['created_at']:%Y-%m-%d %H:%M}")
            print(f"     inferred_type={s.get('inferred_type')}  row_count={s.get('row_count')}"
                  f"  buckets={buckets}")
            print(f"     confirmed_fields={s.get('confirmed_fields')}"
                  f"  imported_counts={s.get('imported_counts')}")
            if s.get("warnings"):
                print(f"     warnings={json.dumps(s['warnings'], ensure_ascii=False)[:300]}")

        # ── 2. GASTOS: por categoría / fechas / forma de pago ─────────────
        p(f"EXPENSE ENTRIES  (tenant {tid})")
        cats = await conn.fetch(
            "SELECT category, count(*) n, sum(amount) total, "
            "min(transaction_date) dmin, max(transaction_date) dmax "
            "FROM expense_entries WHERE tenant_id=$1 AND voided_at IS NULL "
            "GROUP BY category ORDER BY n DESC",
            tid,
        )
        total = sum(r["n"] for r in cats)
        print(f"  total gastos activos: {total}")
        for r in cats:
            print(f"     {r['category']!r:32} n={r['n']:<5} ${r['total']:<14} "
                  f"[{r['dmin']:%Y-%m-%d} .. {r['dmax']:%Y-%m-%d}]")
        extra = await conn.fetch(
            "SELECT provenance, payment_method, count(*) n FROM expense_entries "
            "WHERE tenant_id=$1 AND voided_at IS NULL "
            "GROUP BY provenance, payment_method ORDER BY n DESC",
            tid,
        )
        print("  provenance × payment_method:")
        for r in extra:
            print(f"     {r['provenance']}/{r['payment_method']}: {r['n']}")
        voided = await conn.fetchval(
            "SELECT count(*) FROM expense_entries WHERE tenant_id=$1 AND voided_at IS NOT NULL",
            tid,
        )
        print(f"  anulados (voided): {voided}")

        # ── 3. IMPORTS POR CHAT (pending actions) ─────────────────────────
        p(f"PENDING ACTIONS IMPORT_TABULAR_FILE  (tenant {tid})")
        pa = await conn.fetch(
            "SELECT id, status, execution_status, created_at, payload "
            "FROM pending_actions WHERE tenant_id=$1 AND action_type='IMPORT_TABULAR_FILE' "
            "ORDER BY created_at DESC LIMIT 20",
            tid,
        )
        if not pa:
            print("  (ninguna)")
        for r in pa:
            payload = {}
            if r["payload"]:
                try:
                    raw_pl = r["payload"]
                    payload = json.loads(raw_pl) if isinstance(raw_pl, str) else raw_pl
                except (TypeError, ValueError):
                    payload = {}
            cf = payload.get("confirmed_fields") if isinstance(payload, dict) else None
            fid = payload.get("file_id") if isinstance(payload, dict) else None
            print(f"  {r['created_at']:%Y-%m-%d %H:%M}  status={r['status']} "
                  f"exec={r['execution_status']}  file_id={fid}  confirmed_fields={cf}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: diag_expenses.py <email>")
        sys.exit(2)
    asyncio.run(main(sys.argv[1]))
