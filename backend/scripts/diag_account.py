"""Read-only account diagnostic for ingestion issues.

Usage:
    DATABASE_URL='postgresql://...neon.tech/vektor?sslmode=require' \
        .venv/bin/python scripts/diag_account.py agustinalahora@gmail.com

ONLY runs SELECT statements. No writes. Safe against production.
"""

import asyncio
import json
import os
import sys

import asyncpg
from _db import normalize_dsn

__all__ = ["normalize_dsn", "p"]


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
        "SELECT user_id, tenant_id, email, role_code, is_active, created_at, last_login_at "
        "FROM users WHERE lower(email) = lower($1)",
        email,
    )
    if not users:
        print("  ⚠ No existe ningún usuario con ese email.")
        # fuzzy
        like = await conn.fetch(
            "SELECT email FROM users WHERE email ILIKE $1 LIMIT 10", f"%{email.split('@')[0]}%"
        )
        if like:
            print("  Parecidos:", [r["email"] for r in like])
        return
    for u in users:
        print(f"  user_id={u['user_id']}  tenant_id={u['tenant_id']}  role={u['role_code']} "
              f"active={u['is_active']}  creado={u['created_at']}  last_login={u['last_login_at']}")
    tenant_ids = list({u["tenant_id"] for u in users})

    for tid in tenant_ids:
        t = await conn.fetchrow(
            "SELECT * FROM tenants WHERE tenant_id = $1", tid
        )
        if t:
            # asyncpg.Record itera VALORES (no claves): `for k in t` + `t[k]`
            # reventaba con cualquier valor no-string. Iterar items().
            print(f"\n  TENANT {tid}: " + ", ".join(
                f"{k}={v}" for k, v in dict(t).items() if k != "tenant_id"
            )[:400])
        bp = await conn.fetchrow(
            "SELECT business_type, business_name FROM business_profiles "
            "WHERE tenant_id=$1 LIMIT 1",
            tid,
        )
        if bp:
            print(f"  business: type={bp['business_type']} name={bp['business_name']!r}")

        # ── UPLOADED FILES ────────────────────────────────────────────────
        p(f"UPLOADED FILES  (tenant {tid})")
        files = await conn.fetch(
            "SELECT id, original_filename, content_type, size_bytes, purpose, status, "
            "processing_status, rejection_reason, created_at, parsed_summary_json "
            "FROM uploaded_files WHERE tenant_id=$1 ORDER BY created_at DESC",
            tid,
        )
        print(f"  total archivos: {len(files)}")
        # status breakdown
        from collections import Counter
        by_proc = Counter(f["processing_status"] for f in files)
        by_status = Counter(f["status"] for f in files)
        by_purpose = Counter(f["purpose"] for f in files)
        print(f"  processing_status: {dict(by_proc)}")
        print(f"  status: {dict(by_status)}")
        print(f"  purpose(file_hint): {dict(by_purpose)}")

        for f in files:
            print(f"\n  ── {f['original_filename']!r}  [{f['content_type']}]  {f['size_bytes']}B")
            print(f"     id={f['id']}  purpose={f['purpose']}  status={f['status']} "
                  f"proc={f['processing_status']}  rej={f['rejection_reason']}  {f['created_at']}")
            summ = f["parsed_summary_json"]
            if summ:
                try:
                    s = json.loads(summ) if isinstance(summ, str) else summ
                    keys = list(s.keys()) if isinstance(s, dict) else type(s).__name__
                    print(f"     summary keys: {keys}")
                    if isinstance(s, dict):
                        for bucket in ("ventas", "gastos", "productos", "stock_detectado",
                                       "sales", "expenses", "products"):
                            if bucket in s and isinstance(s[bucket], list):
                                ej = (
                                    f"  ej={json.dumps(s[bucket][0], ensure_ascii=False)[:200]}"
                                    if s[bucket]
                                    else ""
                                )
                                print(f"       {bucket}: {len(s[bucket])} filas{ej}")
                        for meta in ("columns_at_risk", "columns", "row_count", "sheets",
                                     "detected_type", "inferred_type", "__context__",
                                     "mapping_contexts", "warnings", "error"):
                            if meta in s:
                                v = s[meta]
                                print(f"       {meta}: {json.dumps(v, ensure_ascii=False)[:300]}")
                except Exception as e:  # noqa: BLE001
                    print(f"     (summary unparseable: {e})  raw[:200]={str(summ)[:200]}")
            else:
                print("     summary: NULL (no parseado / falló parseo)")

        # ── INGESTED DATA COUNTS ──────────────────────────────────────────
        p(f"DATOS CARGADOS  (tenant {tid})")
        for tbl, label in (("sales_entries", "ventas"), ("expense_entries", "gastos"),
                           ("products", "productos")):
            rows = await conn.fetch(
                f"SELECT provenance, count(*) n, count(voided_at) voided "  # noqa: S608
                f"FROM {tbl} WHERE tenant_id=$1 GROUP BY provenance ORDER BY n DESC",
                tid,
            ) if tbl != "products" else await conn.fetch(
                f"SELECT provenance, count(*) n, count(deactivated_at) voided "  # noqa: S608
                f"FROM {tbl} WHERE tenant_id=$1 GROUP BY provenance ORDER BY n DESC",
                tid,
            )
            total = sum(r["n"] for r in rows)
            detail = ", ".join(f"{r['provenance']}={r['n']}(void {r['voided']})" for r in rows)
            print(f"  {label}: total={total}  [{detail}]")
            # data quality sniff on amounts/dates
            if tbl in ("sales_entries", "expense_entries"):
                q = await conn.fetchrow(
                    f"SELECT min(amount) mn, max(amount) mx, "  # noqa: S608
                    f"count(*) FILTER (WHERE amount = 0) zero, "
                    f"min(transaction_date) dmin, max(transaction_date) dmax "
                    f"FROM {tbl} WHERE tenant_id=$1 AND voided_at IS NULL",
                    tid,
                )
                if q and total:
                    print(f"     amount[{q['mn']}..{q['mx']}] zero={q['zero']} "
                          f"fecha[{q['dmin']}..{q['dmax']}]")

        # ── CUSTOM FIELDS / MAPPINGS ──────────────────────────────────────
        p(f"MAPEOS & CUSTOM FIELDS  (tenant {tid})")
        cm = await conn.fetch(
            "SELECT source_column, target_field, entity_type, confirmed_count "
            "FROM tenant_column_mappings WHERE tenant_id=$1 ORDER BY entity_type", tid,
        )
        print(f"  tenant_column_mappings: {len(cm)}")
        for r in cm[:40]:
            print(f"     [{r['entity_type']}] {r['source_column']!r} → {r['target_field']!r} "
                  f"(visto {r['confirmed_count']}x)")
        tcf = await conn.fetch(
            "SELECT field_key, entity_type, data_type, override_label, is_base_field "
            "FROM tenant_custom_field_definitions WHERE tenant_id=$1", tid,
        )
        print(f"  custom field defs: {len(tcf)}")
        for r in tcf[:40]:
            print(f"     [{r['entity_type']}] {r['field_key']} ({r['data_type']}) "
                  f"{r['override_label']!r} base={r['is_base_field']}")

        # ── PENDING ACTIONS (imports en vuelo / fallidos) ─────────────────
        p(f"PENDING ACTIONS  (tenant {tid})")
        try:
            pa = await conn.fetch(
                "SELECT action_type, status, execution_status, count(*) n "
                "FROM pending_actions WHERE tenant_id=$1 "
                "GROUP BY action_type, status, execution_status ORDER BY n DESC LIMIT 40", tid,
            )
            for r in pa:
                print(f"     {r['action_type']}  status={r['status']} "
                      f"exec={r['execution_status']}  n={r['n']}")
            if not pa:
                print("     (ninguna)")
        except Exception as e:  # noqa: BLE001
            print(f"     (no se pudo leer pending_actions: {e})")


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "agustinalahora@gmail.com"
    asyncio.run(main(target))
