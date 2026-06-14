"""Read-only: solapamiento CSV vs COGS + reconciliación de stock por producto.

Decide dos cosas, sin escribir nada:
  A) ¿Las 810 compras del CSV YA están como COGS (vinieron del xlsx)? → sumar
     duplicaría. Compara (fecha, monto) fila a fila.
  B) Stock como libro de inventario: por producto muestra compras del CSV,
     ventas realizadas, stock actual y stock RECONSTRUIDO previo al import malo
     (current − movimientos del 13/06), para definir la fórmula correcta.

SOLO SELECT + descarga read-only de R2. Nunca imprime la URL.

Usage:
    DATABASE_URL='...' R2_*/S3_* env... \
        .venv/bin/python scripts/diag_compras_overlap.py fabriziosolafx@gmail.com
"""

from __future__ import annotations

import asyncio
import csv
import io
import logging
import os
import sys
from collections import Counter, defaultdict
from datetime import date

import asyncpg
from _db import normalize_dsn

for _n in ("botocore", "boto3", "urllib3", "s3transfer", "app"):
    logging.getLogger(_n).setLevel(logging.WARNING)
os.environ.setdefault("LOG_LEVEL", "WARNING")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.application.services.ingestion_import_service import (  # noqa: E402
    _normalize_name,
    _parse_amount,
    _parse_date,
)

IMPORT_DAY = "2026-06-13"  # día del import malo (compras_mercaderia.csv)


def p(title: str) -> None:
    print(f"\n{'='*70}\n  {title}\n{'='*70}")


def _key(d: object, amt: object) -> tuple[str, str] | None:
    if d is None or amt is None:
        return None
    return (str(d), f"{float(amt):.2f}")  # type: ignore[arg-type]


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

    f = await conn.fetchrow(
        "SELECT id, s3_key FROM uploaded_files WHERE tenant_id=$1 "
        "AND original_filename ILIKE '%compras_mercader%' ORDER BY created_at DESC LIMIT 1",
        tid,
    )
    if not f:
        print("  ⚠ no encontré compras_mercaderia.csv.")
        return

    # ── COGS/INVENTORY existentes (para solapamiento) ─────────────────────
    cogs = await conn.fetch(
        "SELECT date(transaction_date) d, amount FROM expense_entries "
        "WHERE tenant_id=$1 AND voided_at IS NULL "
        "AND (category='INVENTORY' OR expense_type='COGS')",
        tid,
    )
    existing_keys: Counter[tuple[str, str]] = Counter()
    existing_amt: Counter[str] = Counter()
    for r in cogs:
        k = _key(r["d"], r["amount"])
        if k:
            existing_keys[k] += 1
        existing_amt[f"{float(r['amount']):.2f}"] += 1

    # ── Productos + ventas + movimientos del día del import ───────────────
    prods = await conn.fetch(
        "SELECT id, sku, name, stock_units FROM products "
        "WHERE tenant_id=$1 AND deactivated_at IS NULL",
        tid,
    )
    pid_by_sku: dict[str, str] = {}
    pid_by_name: dict[str, str] = {}
    pinfo: dict[str, dict] = {}
    for r in prods:
        pinfo[str(r["id"])] = {"sku": r["sku"], "name": r["name"], "stock": r["stock_units"]}
        if r["sku"]:
            pid_by_sku[str(r["sku"]).strip().lower()] = str(r["id"])
        nm = _normalize_name(r["name"] or "")
        if nm:
            pid_by_name.setdefault(nm, str(r["id"]))

    sales_q = await conn.fetch(
        "SELECT product_id, coalesce(sum(quantity),0) q FROM sales_entries "
        "WHERE tenant_id=$1 AND voided_at IS NULL AND product_id IS NOT NULL GROUP BY product_id",
        tid,
    )
    sold_by_pid: dict[str, int] = {str(r["product_id"]): int(r["q"]) for r in sales_q}

    mv_q = await conn.fetch(
        "SELECT product_id, coalesce(sum(qty),0) q FROM inventory_movements "
        "WHERE tenant_id=$1 AND date(created_at)=$2 GROUP BY product_id",
        tid, date.fromisoformat(IMPORT_DAY),
    )
    mv0613_by_pid: dict[str, int] = {str(r["product_id"]): int(r["q"]) for r in mv_q}

    # ── Crudo del CSV ─────────────────────────────────────────────────────
    from app.integrations.s3 import S3Client  # noqa: PLC0415

    content = await S3Client().download(f["s3_key"])
    text = content.decode("utf-8-sig", errors="replace")
    head = text.split("\n", 1)[0]
    delim = ";" if head.count(";") > head.count(",") else ","
    reader = csv.DictReader(io.StringIO(text), delimiter=delim)

    def col(row: dict, *names: str) -> str:
        for k in row:
            kn = (k or "").lower().strip()
            if any(n in kn for n in names):
                return row[k] or ""
        return ""

    avail = Counter(existing_keys)
    avail_amt = Counter(existing_amt)
    matched_dm = matched_amt = missing = 0
    csv_dates = []
    csv_qty_by_pid: dict[str, int] = defaultdict(int)
    csv_unmatched_pid: dict[str, int] = defaultdict(int)
    n = 0
    for row in reader:
        n += 1
        d = _parse_date(col(row, "fecha"))
        amt = _parse_amount(col(row, "total")) or _parse_amount(col(row, "monto", "importe"))
        sku = (col(row, "sku", "codigo", "código") or "").strip().lower()
        nm = _normalize_name(col(row, "producto", "nombre", "descripcion") or "")
        pid = pid_by_sku.get(sku) or pid_by_name.get(nm)
        try:
            q = int(float(str(col(row, "cantidad", "cant")).strip() or 0))
        except (ValueError, TypeError):
            q = 0
        if pid:
            csv_qty_by_pid[pid] += q
        if d:
            csv_dates.append(d.date())
        k = _key(d.date() if d else None, amt)
        if k and avail.get(k, 0) > 0:
            avail[k] -= 1
            matched_dm += 1
        elif amt is not None and avail_amt.get(f"{float(amt):.2f}", 0) > 0:
            avail_amt[f"{float(amt):.2f}"] -= 1
            matched_amt += 1
            if pid:
                csv_unmatched_pid[pid] += q
        else:
            missing += 1
            if pid:
                csv_unmatched_pid[pid] += q

    p("A) SOLAPAMIENTO  (810 filas del CSV vs COGS existentes)")
    print(f"  filas CSV={n}   COGS existentes={len(cogs)}")
    if csv_dates:
        print(f"  rango fechas CSV   = {min(csv_dates)} .. {max(csv_dates)}")
    drange = await conn.fetchrow(
        "SELECT min(date(transaction_date)) a, max(date(transaction_date)) b "
        "FROM expense_entries WHERE tenant_id=$1 AND voided_at IS NULL "
        "AND (category='INVENTORY' OR expense_type='COGS')", tid,
    )
    print(f"  rango fechas COGS  = {drange['a']} .. {drange['b']}")
    print(f"  ✅ match (fecha+monto) = {matched_dm}")
    print(f"  ~ match (solo monto)   = {matched_amt}")
    print(f"  ❌ faltantes reales    = {missing}")
    if missing == 0 and matched_dm > 0:
        print("  → CSV YA presente como COGS. Sumarlo DUPLICARÍA. NO importar gastos.")
    elif matched_dm + matched_amt == 0:
        print("  → Sin solapamiento: las compras del CSV FALTAN. Se suman como COGS.")
    else:
        print("  → Solapamiento PARCIAL. Importar solo las faltantes con dedup estricto.")

    # ── B) Reconciliación de stock ────────────────────────────────────────
    p("B) RECONCILIACIÓN DE STOCK  (por producto)")
    tot_cur = tot_pre = tot_sold = tot_csv = 0
    sample = []
    for pid, info in pinfo.items():
        cur = info["stock"]
        pre = cur - mv0613_by_pid.get(pid, 0)   # stock previo al import malo
        sold = sold_by_pid.get(pid, 0)
        csvq = csv_qty_by_pid.get(pid, 0)
        tot_cur += cur
        tot_pre += pre
        tot_sold += sold
        tot_csv += csvq
        sample.append((info["sku"], info["name"], pre, csvq, sold, cur))
    print(f"  productos={len(pinfo)}")
    print(f"  Σ stock ACTUAL (post import malo)      = {tot_cur}")
    print(f"  Σ stock PREVIO (reconstruido s/ mov 13/06) = {tot_pre}")
    print(f"  Σ compras CSV (cantidad)               = {tot_csv}")
    print(f"  Σ ventas (quantity, ligadas a producto) = {tot_sold}")
    print("\n  muestra (sku | nombre | stock_previo | compras_csv | ventas | stock_actual):")
    for sku, name, pre, csvq, sold, cur in sorted(sample, key=lambda x: -x[3])[:15]:
        print(f"    {str(sku)[:10]:<10} {str(name)[:22]:<22} pre={pre:<6} csv={csvq:<5} "
              f"ventas={sold:<6} actual={cur}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: DATABASE_URL=... python scripts/diag_compras_overlap.py <email>")
        sys.exit(2)
    asyncio.run(main(sys.argv[1]))
