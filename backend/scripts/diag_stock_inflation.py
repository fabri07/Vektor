"""Read-only: mide la INFLACIÓN de stock por applies repetidos de una relectura.

Cada apply de un libro de compras suma stock (`Product.stock_units += qty`) y
registra un `inventory_movements` por fila; anular los gastos NO revierte ese
stock. Si la relectura se aplicó N veces (p.ej. por timeouts que igual completaban
en el backend), el stock quedó ~N×. Este script lo cuantifica para decidir el fix.

SOLO SELECTs + descarga READ-ONLY del crudo de R2. No escribe nada. Nunca imprime
la connection URL.

Usage:
    DATABASE_URL='postgresql://...neon.tech/vektor?sslmode=require' \
        .venv/bin/python scripts/diag_stock_inflation.py fabriziosolafx@gmail.com [nombre_archivo]

`nombre_archivo` opcional (ILIKE), default 'compras_mercader'.
"""

from __future__ import annotations

import asyncio
import csv
import io
import os
import sys
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


def _norm(s: str) -> str:
    return " ".join((s or "").strip().lower().split())


async def main(email: str, fname: str) -> None:
    raw = os.environ.get("DATABASE_URL") or os.environ.get("DATABASE_URL_SYNC")
    if not raw:
        print("ERROR: exportá DATABASE_URL con la URL de Neon antes de correr.")
        sys.exit(2)
    dsn, sslarg = normalize_dsn(raw)
    conn = await asyncpg.connect(dsn, ssl=sslarg)
    try:
        await run(conn, email, fname)
    finally:
        await conn.close()


async def run(conn: asyncpg.Connection, email: str, fname: str) -> None:
    u = await conn.fetchrow(
        "SELECT tenant_id FROM users WHERE lower(email)=lower($1) LIMIT 1", email
    )
    if not u:
        print("  ⚠ No existe usuario con ese email.")
        return
    tid = u["tenant_id"]
    print(f"  tenant_id={tid}")

    f = await conn.fetchrow(
        "SELECT id, original_filename, s3_key, created_at FROM uploaded_files "
        "WHERE tenant_id=$1 AND original_filename ILIKE $2 "
        "ORDER BY created_at DESC LIMIT 1",
        tid, f"%{fname}%",
    )
    if not f:
        print(f"  ⚠ No encontré archivo ILIKE '%{fname}%' para este tenant.")
        return
    fid = f["id"]
    print(f"  file_id={fid}  {f['original_filename']!r}  {f['created_at']}")

    p("1) GASTOS de este archivo (activos vs anulados) = evidencia de applies repetidos")
    rows = await conn.fetch(
        "SELECT voided_at IS NULL AS activo, count(*) n FROM expense_entries "
        "WHERE tenant_id=$1 AND source_upload_id=$2 GROUP BY (voided_at IS NULL)",
        tid, fid,
    )
    activos = next((r["n"] for r in rows if r["activo"]), 0)
    anulados = next((r["n"] for r in rows if not r["activo"]), 0)
    print(f"  activos={activos}   anulados={anulados}")
    if activos:
        print(f"  → applies que completaron ≈ {round((activos + anulados) / activos)} "
              f"(anulados/activos = {anulados}/{activos})")

    p("2) MOVIMIENTOS de stock tipo 'purchase' (cada apply registró uno por fila)")
    mv = await conn.fetchrow(
        "SELECT count(*) n, coalesce(sum(qty),0) sum_qty FROM inventory_movements "
        "WHERE tenant_id=$1 AND movement_type='purchase'",
        tid,
    )
    print(f"  movimientos purchase = {mv['n']}   Σ qty aplicada = {mv['sum_qty']}")
    print("  (si Σ qty ≈ N × Σ qty del archivo → stock inflado N×)")

    p("3) CRUDO R2: cantidad real por producto (UN import)")
    truth_by_sku: dict[str, Decimal] = {}
    truth_by_name: dict[str, Decimal] = {}
    file_qty_total = Decimal(0)
    try:
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
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

        for row in reader:
            q = _num(col(row, "cantidad", "cant", "unidades"))
            if q is None:
                continue
            file_qty_total += q
            sku = _norm(col(row, "sku", "codigo", "código"))
            name = _norm(col(row, "producto", "nombre", "descripcion", "detalle", "articulo"))
            if sku:
                truth_by_sku[sku] = truth_by_sku.get(sku, Decimal(0)) + q
            if name:
                truth_by_name[name] = truth_by_name.get(name, Decimal(0)) + q
        print(f"  Σ cantidad del archivo (1 import) = {file_qty_total}")
        print(f"  productos distintos por SKU={len(truth_by_sku)} por nombre={len(truth_by_name)}")
        if file_qty_total and mv["sum_qty"]:
            print(f"  → factor de inflación ≈ {Decimal(mv['sum_qty']) / file_qty_total:.2f}×")
    except Exception as exc:  # noqa: BLE001
        print(f"  (no se pudo leer R2 — {type(exc).__name__}: {exc})")
        print("  → corré con las env vars R2_*/S3_* para el cálculo por producto.")

    p("4) PRODUCTOS: stock actual vs cantidad del archivo (top 20 por gap)")
    prods = await conn.fetch(
        "SELECT id, name, sku, stock_units, requires_completion FROM products "
        "WHERE tenant_id=$1 AND deactivated_at IS NULL",
        tid,
    )
    gaps: list[tuple[str, int, Decimal, Decimal]] = []
    for pr in prods:
        truth = None
        if pr["sku"] and _norm(pr["sku"]) in truth_by_sku:
            truth = truth_by_sku[_norm(pr["sku"])]
        elif _norm(pr["name"]) in truth_by_name:
            truth = truth_by_name[_norm(pr["name"])]
        if truth is None:
            continue
        cur = Decimal(pr["stock_units"] or 0)
        gap = cur - truth
        if gap != 0:
            gaps.append((pr["name"], pr["stock_units"], truth, gap))
    gaps.sort(key=lambda t: abs(t[3]), reverse=True)
    print(f"  productos con gap (stock_units ≠ cantidad del archivo): {len(gaps)}")
    print(f"  {'producto':<34} {'stock_actual':>12} {'archivo':>9} {'gap':>9}")
    for name, cur, truth, gap in gaps[:20]:
        print(f"  {name[:34]:<34} {cur:>12} {truth:>9} {gap:>9}")
    if gaps:
        ratios = [Decimal(cur) / truth for _, cur, truth, _ in gaps if truth]
        if ratios:
            avg = sum(ratios) / len(ratios)
            print(f"\n  ratio promedio stock_actual/archivo ≈ {avg:.2f}× "
                  f"(≈ nº de applies; si ≈1 NO hay inflación)")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: DATABASE_URL=... python scripts/diag_stock_inflation.py <email> [archivo]")
        sys.exit(2)
    fname = sys.argv[2] if len(sys.argv) > 2 else "compras_mercader"
    asyncio.run(main(sys.argv[1], fname))
