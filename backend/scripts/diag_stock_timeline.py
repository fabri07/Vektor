"""Read-only: cuenta corrida de stock por producto, ordenada por fecha REAL.

Mezcla compras del CSV (+cantidad, por su `fecha`) y ventas (−quantity, por su
`transaction_date`), ordena cronológicamente por producto y calcula el balance
corrido desde 0. El mínimo (más negativo) = stock inicial implícito que el
producto debía tener antes del primer registro. Así se ve si los negativos son
reales o sólo artefacto del agregado all-time.

SOLO SELECT + descarga read-only de R2. Nunca imprime la URL.

Usage:
    DATABASE_URL='...' R2_*/S3_* env... \
        .venv/bin/python scripts/diag_stock_timeline.py fabriziosolafx@gmail.com
"""

from __future__ import annotations

import asyncio
import csv
import io
import logging
import os
import sys
from collections import defaultdict

import asyncpg
from _db import normalize_dsn

for _n in ("botocore", "boto3", "urllib3", "s3transfer", "app"):
    logging.getLogger(_n).setLevel(logging.WARNING)
os.environ.setdefault("LOG_LEVEL", "WARNING")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.application.services.ingestion_import_service import (  # noqa: E402
    _normalize_name,
    _parse_date,
)


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

    f = await conn.fetchrow(
        "SELECT id, s3_key FROM uploaded_files WHERE tenant_id=$1 "
        "AND original_filename ILIKE '%compras_mercader%' ORDER BY created_at DESC LIMIT 1",
        tid,
    )
    if not f:
        print("  ⚠ no encontré compras_mercaderia.csv.")
        return

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

    # eventos por producto: (fecha_date, orden, delta)  orden 0=compra,1=venta
    events: dict[str, list[tuple]] = defaultdict(list)

    sales = await conn.fetch(
        "SELECT product_id, date(transaction_date) d, quantity FROM sales_entries "
        "WHERE tenant_id=$1 AND voided_at IS NULL AND product_id IS NOT NULL",
        tid,
    )
    for r in sales:
        events[str(r["product_id"])].append((r["d"], 1, -int(r["quantity"] or 0)))

    # compras del CSV
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

    csv_no_prod = 0
    for row in reader:
        d = _parse_date(col(row, "fecha"))
        sku = (col(row, "sku", "codigo", "código") or "").strip().lower()
        nm = _normalize_name(col(row, "producto", "nombre", "descripcion") or "")
        pid = pid_by_sku.get(sku) or pid_by_name.get(nm)
        try:
            q = int(float(str(col(row, "cantidad", "cant")).strip() or 0))
        except (ValueError, TypeError):
            q = 0
        if not pid:
            csv_no_prod += 1
            continue
        events[pid].append((d.date() if d else None, 0, q))

    # ── Cuenta corrida por producto ───────────────────────────────────────
    rows_out = []
    n_neg = 0
    tot_open = 0
    tot_recon = 0
    for pid, info in pinfo.items():
        evs = events.get(pid, [])
        # ordenar por fecha; misma fecha → compras (0) antes que ventas (1);
        # fechas None van primero (se asume movimiento más viejo).
        evs.sort(key=lambda e: (e[0] is not None, e[0], e[1]))
        bal = 0
        min_bal = 0
        min_date = None
        buys = sells = 0
        for d, kind, delta in evs:
            bal += delta
            if kind == 0:
                buys += delta
            else:
                sells += -delta
            if bal < min_bal:
                min_bal = bal
                min_date = d
        implied_open = max(0, -min_bal)
        recon_current = implied_open + bal  # opening + (Σcompras − Σventas)
        tot_open += implied_open
        tot_recon += recon_current
        if min_bal < 0:
            n_neg += 1
        rows_out.append((info["sku"], info["name"], buys, sells, min_bal,
                         min_date, implied_open, recon_current, info["stock"]))

    p("CUENTA CORRIDA POR PRODUCTO  (ordenada por fecha real)")
    print(f"  productos={len(pinfo)}  filas CSV sin producto={csv_no_prod}")
    print(f"  productos que tocan negativo (con opening=0) = {n_neg}")
    print(f"  Σ stock inicial implícito (para que nada quede <0) = {tot_open}")
    print(f"  Σ stock reconstruido (opening + compras − ventas)  = {tot_recon}")
    print(f"  Σ stock actual en DB (post import malo)            = "
          f"{sum(i['stock'] for i in pinfo.values())}")
    print("\n  muestra (sku | nombre | compras | ventas | min_bal | fecha_min | "
          "opening | reconstruido | stock_db):")
    for sku, name, buys, sells, mn, md, op, rec, st in sorted(
        rows_out, key=lambda x: x[4]
    )[:20]:
        print(f"    {str(sku)[:9]:<9} {str(name)[:20]:<20} c={buys:<5} v={sells:<5} "
              f"min={mn:<6} {str(md):<11} op={op:<5} rec={rec:<5} db={st}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: DATABASE_URL=... python scripts/diag_stock_timeline.py <email>")
        sys.exit(2)
    asyncio.run(main(sys.argv[1]))
