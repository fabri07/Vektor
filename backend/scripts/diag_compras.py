"""Read-only diagnostic for the misclassified 'compras_mercaderia.csv' import.

Usage:
    DATABASE_URL='postgresql://...neon.tech/vektor?sslmode=require' \
        .venv/bin/python scripts/diag_compras.py fabriziosolafx@gmail.com

ONLY SELECTs + a READ-ONLY download of the raw file from R2. No writes.
Nunca imprime la connection URL. Reporta:
  1. summary del archivo (imported_counts / confirmed_fields / inferred_type)
  2. inventory_movements aplicados el día del import (stock que se pisó)
  3. estado actual de productos (stock_units, requires_completion)
  4. gastos COGS/INVENTORY ya presentes (para ver el hueco real)
  5. (si hay R2) análisis del crudo: filas, $ total de compra, split de
     forma_pago (cuánto es efectivo → caja), SKUs distintos, suma de cantidad
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import os
import sys
from collections import Counter
from decimal import Decimal

import asyncpg
from _db import normalize_dsn


def p(title: str) -> None:
    print(f"\n{'='*70}\n  {title}\n{'='*70}")


def _num(raw: str) -> Decimal | None:
    s = str(raw).strip().replace("$", "").replace(" ", "")
    if not s:
        return None
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        return Decimal(s)
    except Exception:  # noqa: BLE001
        return None


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
    u = await conn.fetchrow(
        "SELECT user_id, tenant_id FROM users WHERE lower(email)=lower($1) LIMIT 1", email
    )
    if not u:
        print("  ⚠ No existe usuario con ese email.")
        return
    tid = u["tenant_id"]
    print(f"  tenant_id={tid}")

    f = await conn.fetchrow(
        "SELECT id, original_filename, s3_key, content_type, created_at, parsed_summary_json "
        "FROM uploaded_files WHERE tenant_id=$1 AND original_filename ILIKE '%compras_mercader%' "
        "ORDER BY created_at DESC LIMIT 1",
        tid,
    )
    if not f:
        print("  ⚠ No encontré 'compras_mercaderia.csv' para este tenant.")
        return

    p("1) SUMMARY DEL ARCHIVO")
    print(f"  file_id={f['id']}  {f['original_filename']!r}  {f['created_at']}")
    summ = f["parsed_summary_json"]
    s = json.loads(summ) if isinstance(summ, str) else (summ or {})
    print(f"  inferred_type = {s.get('inferred_type')!r}   ← 'stock' = mal clasificado")
    print(f"  delimiter={s.get('delimiter')!r}  row_count={s.get('row_count')}")
    print(f"  imported_counts  = {s.get('imported_counts')}")
    print(f"  confirmed_fields = {s.get('confirmed_fields')}")

    p("2) INVENTORY_MOVEMENTS del día del import (stock que se aplicó)")
    day = f["created_at"].date()
    mv = await conn.fetch(
        "SELECT movement_type, count(*) n, coalesce(sum(qty),0) sum_qty "
        "FROM inventory_movements WHERE tenant_id=$1 AND date(created_at)=$2 "
        "GROUP BY movement_type ORDER BY n DESC",
        tid, day,
    )
    if not mv:
        print(f"  (sin movimientos el {day})")
    for r in mv:
        print(f"  {r['movement_type']:<12} n={r['n']:<6} sum_qty={r['sum_qty']}")

    p("3) PRODUCTOS actuales")
    pr = await conn.fetchrow(
        "SELECT count(*) n, coalesce(sum(stock_units),0) stock, "
        "count(*) FILTER (WHERE requires_completion) needs, "
        "count(*) FILTER (WHERE unit_cost_ars IS NULL) no_cost "
        "FROM products WHERE tenant_id=$1 AND deactivated_at IS NULL",
        tid,
    )
    print(f"  productos={pr['n']}  sum(stock_units)={pr['stock']}  "
          f"requires_completion={pr['needs']}  sin_costo={pr['no_cost']}")

    p("4) GASTOS COGS / mercadería ya presentes")
    g = await conn.fetch(
        "SELECT expense_type, count(*) n, coalesce(sum(amount),0) total "
        "FROM expense_entries WHERE tenant_id=$1 AND voided_at IS NULL "
        "GROUP BY expense_type ORDER BY total DESC",
        tid,
    )
    for r in g:
        print(f"  expense_type={r['expense_type']!r:<8} n={r['n']:<5} total={r['total']}")
    inv = await conn.fetchrow(
        "SELECT count(*) n, coalesce(sum(amount),0) total FROM expense_entries "
        "WHERE tenant_id=$1 AND voided_at IS NULL AND category='INVENTORY'",
        tid,
    )
    print(f"  category=INVENTORY: n={inv['n']} total={inv['total']}")

    p("5) ANÁLISIS DEL CRUDO (R2, read-only)")
    try:
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from app.integrations.s3 import S3Client  # noqa: PLC0415

        content = await S3Client().download(f["s3_key"])
    except Exception as exc:  # noqa: BLE001
        print(f"  (no se pudo leer R2 — {type(exc).__name__}: {exc})")
        print("  → corré con las env vars R2_*/S3_* para el análisis del crudo.")
        return

    text = content.decode("utf-8-sig", errors="replace")
    delim = ";" if text.split("\n", 1)[0].count(";") > text.split("\n", 1)[0].count(",") else ","
    reader = csv.DictReader(io.StringIO(text), delimiter=delim)
    rows = list(reader)
    print(f"  filas={len(rows)}  delimiter={delim!r}  columnas={reader.fieldnames}")

    def col(row: dict, *names: str) -> str:
        for k in row:
            kn = (k or "").lower().strip()
            if any(n in kn for n in names):
                return row[k] or ""
        return ""

    total_sum = Decimal(0)
    qty_sum = Decimal(0)
    pagos: Counter[str] = Counter()
    pagos_monto: dict[str, Decimal] = {}
    skus: set[str] = set()
    parseables = 0
    for row in rows:
        amt = _num(col(row, "total")) or _num(col(row, "monto", "importe"))
        q = _num(col(row, "cantidad", "cant"))
        pago = (col(row, "forma_pago", "pago", "medio") or "(vacío)").strip().lower()
        sku = (col(row, "sku", "codigo", "código") or "").strip()
        if sku:
            skus.add(sku)
        if amt is not None:
            parseables += 1
            total_sum += amt
            pagos[pago] += 1
            pagos_monto[pago] = pagos_monto.get(pago, Decimal(0)) + amt
        if q is not None:
            qty_sum += q
    print(f"  filas con monto parseable={parseables}")
    print(f"  Σ total (compra $) = {total_sum}")
    print(f"  Σ cantidad         = {qty_sum}")
    print(f"  SKUs distintos     = {len(skus)}")
    print("  forma_pago (n / $):")
    for pago, n in pagos.most_common():
        print(f"    {pago:<16} n={n:<5} $={pagos_monto.get(pago, 0)}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: DATABASE_URL=... python scripts/diag_compras.py <email>")
        sys.exit(2)
    asyncio.run(main(sys.argv[1]))
