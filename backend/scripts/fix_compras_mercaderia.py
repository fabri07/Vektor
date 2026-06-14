"""Fix de datos: reconcilia compras_mercaderia.csv en don pedro.

Qué hace (idempotente, reversible, dry-run por default):

  GASTOS/COGS + CAJA — reconcilia las 810 filas del CSV contra los COGS ya
  cargados por (fecha, monto):
    · fila YA presente  → corrige payment_method desde forma_pago real (guarda
      el valor viejo en custom_fields._orig_payment_method), asegura COGS +
      vínculo a producto + proveedor. Tag _fix_batch.
    · fila FALTANTE     → inserta ExpenseEntry COGS con forma_pago real, producto
      y proveedor. Tag _fix_batch + _fix_op=insert.
  El método de pago canónico hace que SOLO el efectivo impacte caja física
  (transfer→banco, account→proveedores), igual que el modelo contable.

  STOCK — revierte los inventory_movements del import malo (13/06,
  source_event_id='import') con movimientos compensatorios (audit insert-only),
  restaurando el stock confiable previo (~4135). NO re-suma las compras del CSV
  (ya están dentro de ese snapshot).

NO impacta stock desde las compras (ya resuelto por la reversa). Nunca imprime
la URL. Correr desde backend/ con DATABASE_URL + R2_*/S3_* en el env.

Usage:
    # Dry-run (no escribe):
    DATABASE_URL=... R2_*... .venv/bin/python scripts/fix_compras_mercaderia.py <email>
    # Aplicar:
    ... scripts/fix_compras_mercaderia.py fabriziosolafx@gmail.com --apply
    # Revertir un apply previo:
    ... scripts/fix_compras_mercaderia.py fabriziosolafx@gmail.com --undo --apply
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import io
import logging
import os
import sys
import uuid
from collections import Counter, defaultdict
from datetime import date
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _db import async_engine_config  # noqa: E402
from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine  # noqa: E402

from app.application.services.cash_service import normalize_payment_method  # noqa: E402
from app.application.services.ingestion_import_service import (  # noqa: E402
    _load_product_index,
    _parse_amount,
    _parse_date,
    _resolve_product,
)
from app.domain.expense_categories import (  # noqa: E402
    classify_expense_with_vertical,
    infer_expense_type,
)
from app.persistence.models.inventory import InventoryBalance, InventoryMovement  # noqa: E402
from app.persistence.models.product import Product  # noqa: E402
from app.persistence.models.transaction import ExpenseEntry  # noqa: E402

for _n in ("botocore", "boto3", "urllib3", "s3transfer", "app"):
    logging.getLogger(_n).setLevel(logging.WARNING)
os.environ.setdefault("LOG_LEVEL", "WARNING")

BATCH_TAG = "compras_fix_20260614"
IMPORT_DAY = date(2026, 6, 13)
# caja física solo efectivo; transfer/qr/debit = banco/digital; account = proveedores.
_CASH = {"cash"}
_BANK = {"transfer", "qr", "debit_card", "credit_card"}
_AP = {"account"}


def p(t: str) -> None:
    print(f"\n{'='*70}\n  {t}\n{'='*70}")


def _key(d: date | None, amt: Decimal | None) -> tuple[str, str] | None:
    if d is None or amt is None:
        return None
    return (d.isoformat(), f"{float(amt):.2f}")


def _col(row: dict, *names: str) -> str:
    for k in row:
        kn = (k or "").lower().strip()
        if any(n in kn for n in names):
            return row[k] or ""
    return ""


async def _load_csv_rows(s3_key: str) -> list[dict]:
    from app.integrations.s3 import S3Client  # noqa: PLC0415

    content = await S3Client().download(s3_key)
    text = content.decode("utf-8-sig", errors="replace")
    head = text.split("\n", 1)[0]
    delim = ";" if head.count(";") > head.count(",") else ","
    return list(csv.DictReader(io.StringIO(text), delimiter=delim))


async def _undo(session: AsyncSession, tid: uuid.UUID) -> None:
    p("UNDO — revierto el fix previo")
    exps = (
        await session.execute(
            select(ExpenseEntry).where(
                ExpenseEntry.tenant_id == tid,
                ExpenseEntry.custom_fields["_fix_batch"].astext == BATCH_TAG,
            )
        )
    ).scalars().all()
    deleted = restored = 0
    for e in exps:
        cf = dict(e.custom_fields or {})
        if cf.get("_fix_op") == "insert":
            await session.delete(e)
            deleted += 1
        else:
            if "_orig_payment_method" in cf:
                e.payment_method = cf["_orig_payment_method"]
                restored += 1
            for k in ("_fix_batch", "_orig_payment_method", "_fix_op"):
                cf.pop(k, None)
            e.custom_fields = cf
    # stock: revertir los movimientos compensatorios
    comps = (
        await session.execute(
            select(InventoryMovement).where(
                InventoryMovement.tenant_id == tid,
                InventoryMovement.source_event_id == BATCH_TAG,
            )
        )
    ).scalars().all()
    bal_index = {
        b.product_id: b
        for b in (
            await session.execute(
                select(InventoryBalance).where(InventoryBalance.tenant_id == tid)
            )
        ).scalars().all()
    }
    reverted = 0
    for m in comps:
        prod = await session.get(Product, m.product_id)
        if prod is not None:
            prod.stock_units -= m.qty  # deshacer el delta compensatorio
        b = bal_index.get(m.product_id)
        if b is not None:
            b.current_qty -= m.qty
        await session.delete(m)
        reverted += 1
    print(f"  gastos borrados={deleted}  payment_method restaurados={restored}  "
          f"movimientos compensatorios revertidos={reverted}")


async def run(session: AsyncSession, email: str, apply: bool, undo: bool) -> None:
    from sqlalchemy import text as _text  # noqa: PLC0415

    trow = (
        await session.execute(
            _text("SELECT tenant_id FROM users WHERE lower(email)=lower(:e) LIMIT 1"),
            {"e": email},
        )
    ).first()
    if not trow:
        print("  ⚠ usuario inexistente.")
        return
    tid = trow[0]

    if undo:
        await _undo(session, tid)
        if apply:
            await session.commit()
            print("\nUNDO COMMIT aplicado.")
        else:
            await session.rollback()
            print("\nUNDO dry-run: nada se escribió.")
        return

    biz = (
        await session.execute(
            _text("SELECT vertical_code FROM business_profiles WHERE tenant_id=:t"),
            {"t": tid},
        )
    ).first()
    business_type = biz[0] if biz else None

    frow = (
        await session.execute(
            _text(
                "SELECT id, s3_key FROM uploaded_files WHERE tenant_id=:t "
                "AND original_filename ILIKE '%compras_mercader%' "
                "ORDER BY created_at DESC LIMIT 1"
            ),
            {"t": tid},
        )
    ).first()
    if not frow:
        print("  ⚠ no encontré compras_mercaderia.csv.")
        return

    rows = await _load_csv_rows(frow[1])
    by_sku, by_name = await _load_product_index(session, tid)

    # COGS existentes indexados por (fecha, monto)
    existing = (
        await session.execute(
            select(ExpenseEntry).where(
                ExpenseEntry.tenant_id == tid,
                ExpenseEntry.voided_at.is_(None),
                (ExpenseEntry.category == "INVENTORY") | (ExpenseEntry.expense_type == "COGS"),
            )
        )
    ).scalars().all()
    idx: dict[tuple[str, str], list[ExpenseEntry]] = defaultdict(list)
    for e in existing:
        k = _key(e.transaction_date.date(), e.amount)
        if k:
            idx[k].append(e)

    n_updated = n_pay_fixed = n_inserted = 0
    pay_counts: Counter[str] = Counter()
    pay_money: dict[str, Decimal] = defaultdict(lambda: Decimal(0))

    for row in rows:
        d = _parse_date(_col(row, "fecha"))
        amt = _parse_amount(_col(row, "total")) or _parse_amount(_col(row, "monto", "importe"))
        if amt is None:
            continue
        pay = normalize_payment_method(_col(row, "forma_pago", "pago", "medio") or None)
        name = _col(row, "producto", "nombre", "descripcion") or None
        sku = _col(row, "sku", "codigo", "código") or None
        proveedor = (_col(row, "proveedor") or "").strip()[:300] or None
        cat_code, cat_label, _ = classify_expense_with_vertical(
            (_col(row, "categoria", "rubro") or None), business_type
        )
        product_id = _resolve_product(by_sku, by_name, name, sku)
        pay_counts[pay] += 1
        pay_money[pay] += amt

        k = _key(d.date() if d else None, amt)
        match = idx.get(k, [])
        if match:
            e = match.pop(0)
            cf = dict(e.custom_fields or {})
            if e.payment_method != pay:
                cf["_orig_payment_method"] = e.payment_method
                e.payment_method = pay
                n_pay_fixed += 1
            if e.expense_type != "COGS":
                e.expense_type = infer_expense_type(cat_code, product_id=product_id or e.product_id)
            if e.product_id is None and product_id is not None:
                e.product_id = product_id
            if not e.supplier_name and proveedor:
                e.supplier_name = proveedor
            cf["_fix_batch"] = BATCH_TAG
            cf["_fix_op"] = "update"
            e.custom_fields = cf
            n_updated += 1
        else:
            tx = d if d else None
            exp = ExpenseEntry(
                tenant_id=tid,
                amount=amt,
                category=cat_code,
                expense_type=infer_expense_type(cat_code, product_id=product_id),
                transaction_date=tx or _parse_date("2026-06-03"),  # fallback raro
                description=(f"{name or 'Compra mercadería'}"
                            f"{' — ' + proveedor if proveedor else ''}")[:500],
                is_recurring=False,
                payment_method=pay,
                supplier_name=proveedor,
                provenance="REAL",
                product_id=product_id,
                custom_fields={
                    "_fix_batch": BATCH_TAG,
                    "_fix_op": "insert",
                    **({"category_label": cat_label} if cat_label else {}),
                },
            )
            session.add(exp)
            n_inserted += 1

    # ── STOCK: revertir el import malo del 13/06 ──────────────────────────
    movs = (
        await session.execute(
            select(InventoryMovement).where(
                InventoryMovement.tenant_id == tid,
                func.date(InventoryMovement.created_at) == IMPORT_DAY,
                InventoryMovement.source_event_id == "import",
            )
        )
    ).scalars().all()
    net_by_prod: dict[uuid.UUID, int] = defaultdict(int)
    for m in movs:
        net_by_prod[m.product_id] += m.qty
    bal_index = {
        b.product_id: b
        for b in (
            await session.execute(
                select(InventoryBalance).where(InventoryBalance.tenant_id == tid)
            )
        ).scalars().all()
    }
    delta_total = 0
    n_stock = 0
    for pid, net in net_by_prod.items():
        if net == 0:
            continue
        prod = await session.get(Product, pid)
        if prod is None:
            continue
        prod.stock_units -= net  # restaurar pre-import
        delta_total += -net
        b = bal_index.get(pid)
        if b is not None:
            b.current_qty -= net
        session.add(
            InventoryMovement(
                tenant_id=tid,
                product_id=pid,
                movement_type="adjustment",
                qty=-net,
                unit_cost=None,
                source_event_id=BATCH_TAG,
                reason="reversa import 13/06 mal clasificado (fix compras)",
            )
        )
        n_stock += 1

    # ── Reporte ───────────────────────────────────────────────────────────
    p("RESULTADO (gastos / caja)")
    print(f"  filas CSV procesadas        = {len(rows)}")
    print(f"  COGS existentes actualizados = {n_updated}  "
          f"(de los cuales pago corregido: {n_pay_fixed})")
    print(f"  COGS nuevos insertados       = {n_inserted}")
    caja = sum(pay_money[m] for m in _CASH)
    banco = sum(pay_money[m] for m in _BANK)
    ap = sum(pay_money[m] for m in _AP)
    print("\n  Desglose por destino contable (todas las compras del CSV):")
    print(f"    CAJA física (efectivo)       n={sum(pay_counts[m] for m in _CASH):<5} $={caja}")
    print(f"    BANCO/Digital (transf/qr/..) n={sum(pay_counts[m] for m in _BANK):<5} $={banco}")
    print(f"    PROVEEDORES (cuenta cte)     n={sum(pay_counts[m] for m in _AP):<5} $={ap}")
    other = {k: v for k, v in pay_counts.items() if k not in _CASH | _BANK | _AP}
    if other:
        print(f"    OTROS                        {dict(other)}")

    p("RESULTADO (stock)")
    print(f"  productos con stock restaurado = {n_stock}")
    print(f"  movimientos compensatorios     = {n_stock}")
    print(f"  Δ total stock (debería ≈ +397 hacia 4135) = {delta_total}")

    if apply:
        await session.commit()
        print("\nCOMMIT aplicado. Recordá correr refresh_health_score.py para el tenant.")
    else:
        await session.rollback()
        print("\nDRY-RUN: nada se escribió. Reejecutá con --apply para aplicar.")


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("email")
    ap.add_argument("--apply", action="store_true", help="Escribir (default: dry-run)")
    ap.add_argument("--undo", action="store_true", help="Revertir un apply previo (por _fix_batch)")
    args = ap.parse_args()

    url, connect_args = async_engine_config()
    engine = create_async_engine(url, connect_args=connect_args)
    async with AsyncSession(engine) as session:
        await run(session, args.email, args.apply, args.undo)
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
