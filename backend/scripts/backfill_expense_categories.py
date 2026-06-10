"""Backfill de categorías canónicas y expense_type en datos ya importados.

Usage:
    # Dry-run (default): muestra qué cambiaría, no escribe nada.
    DATABASE_URL='postgresql://...' .venv/bin/python scripts/backfill_expense_categories.py

    # Aplicar a un tenant puntual (piloto) o a todos:
    ... scripts/backfill_expense_categories.py --apply --tenant <uuid>
    ... scripts/backfill_expense_categories.py --apply --all
    # Incluir productos:
    ... scripts/backfill_expense_categories.py --products [--apply ...]

Qué hace (por tenant):
  1. expense_entries con category fuera del catálogo canónico → normaliza
     (alias es-AR / fuzzy); una categoría que matchea el catálogo de PRODUCTOS
     del vertical ("Bebidas", "Golosinas") es compra de mercadería → INVENTORY;
     el texto original se preserva en custom_fields.category_label cuando cae a
     OTHER o a INVENTORY-por-producto. Reversible.
  2. expense_type: COGS donde category == INVENTORY o product_id IS NOT NULL
     (solo si la columna existe — correr DESPUÉS de la migración 20260710_0001).
  3. payment_method fuera del catálogo canónico (efectivo→cash, mercadopago→qr,
     cuenta_corriente→account...) en ventas Y gastos; sin match → other con el
     texto original en custom_fields.payment_method_label.
  4. --products: products.category fuera del catálogo del vertical → normaliza.

NUNCA imprime la connection URL. Requiere correr desde backend/ (importa app.domain).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import uuid
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _db import async_engine_config  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine  # noqa: E402

from app.application.services.cash_service import normalize_payment_method  # noqa: E402
from app.domain.expense_categories import (  # noqa: E402
    EXPENSE_CATEGORY_CODES,
    classify_expense_with_vertical,
)
from app.domain.product_categories import (  # noqa: E402
    PRODUCT_CATEGORY_LABELS,
    normalize_product_category,
)
from app.domain.transaction import PaymentMethod  # noqa: E402

_PAYMENT_CODES = {m.value for m in PaymentMethod}


async def _tenant_ids(session: AsyncSession, tenant: str | None) -> list[uuid.UUID]:
    if tenant:
        return [uuid.UUID(tenant)]
    rows = await session.execute(text("SELECT tenant_id FROM tenants ORDER BY created_at"))
    return [r[0] for r in rows.all()]


async def _load_vertical(session: AsyncSession, tid: uuid.UUID) -> str | None:
    return (
        await session.execute(
            text("SELECT vertical_code FROM business_profiles WHERE tenant_id = :tid"),
            {"tid": tid},
        )
    ).scalar_one_or_none()


async def backfill_expenses(session: AsyncSession, tid: uuid.UUID, apply: bool) -> None:
    canonical = set(EXPENSE_CATEGORY_CODES)
    vertical = await _load_vertical(session, tid)
    rows = (
        await session.execute(
            text(
                "SELECT id, category, custom_fields FROM expense_entries "
                "WHERE tenant_id = :tid AND voided_at IS NULL"
            ),
            {"tid": tid},
        )
    ).all()
    plan: Counter[tuple[str, str]] = Counter()
    for row_id, category, _cf in rows:
        if category in canonical:
            continue
        # Vertical-aware: "Bebidas"/"Golosinas" (catálogo de productos) es
        # compra de mercadería → INVENTORY (COGS), no OTHER.
        code, label, _ = classify_expense_with_vertical(category, vertical)
        plan[(category, code)] += 1
        if apply:
            await session.execute(
                text(
                    "UPDATE expense_entries SET category = :code, "
                    "custom_fields = custom_fields || "
                    "CASE WHEN :label IS NOT NULL THEN "
                    "jsonb_build_object('category_label', CAST(:label AS TEXT)) "
                    "ELSE '{}'::jsonb END "
                    "WHERE id = :id"
                ),
                {"code": code, "label": label or category, "id": row_id},
            )
    for (raw, code), n in sorted(plan.items()):
        print(f"    gasto  {raw!r:40} → {code:24} ×{n}")

    # expense_type COGS (solo si la columna existe)
    has_col = (
        await session.execute(
            text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name='expense_entries' AND column_name='expense_type'"
            )
        )
    ).first()
    if has_col:
        count = (
            await session.execute(
                text(
                    "SELECT count(*) FROM expense_entries WHERE tenant_id = :tid "
                    "AND voided_at IS NULL AND expense_type = 'OPEX' "
                    "AND (category = 'INVENTORY' OR product_id IS NOT NULL)"
                ),
                {"tid": tid},
            )
        ).scalar_one()
        print(f"    expense_type → COGS: {count} filas")
        if apply and count:
            await session.execute(
                text(
                    "UPDATE expense_entries SET expense_type = 'COGS' "
                    "WHERE tenant_id = :tid AND voided_at IS NULL "
                    "AND expense_type = 'OPEX' "
                    "AND (category = 'INVENTORY' OR product_id IS NOT NULL)"
                ),
                {"tid": tid},
            )
    else:
        print("    (columna expense_type ausente — correr la migración 20260710_0001 primero)")


async def backfill_payment_methods(session: AsyncSession, tid: uuid.UUID, apply: bool) -> None:
    """Normaliza payment_method crudo ('efectivo', 'mercadopago', 'cuenta_corriente')
    al catálogo canónico en ventas y gastos. Sin match → 'other' preservando el
    texto original en custom_fields.payment_method_label (reversible)."""
    for tbl, label in (("sales_entries", "venta"), ("expense_entries", "gasto")):
        rows = (
            await session.execute(
                text(
                    f"SELECT payment_method, count(*) FROM {tbl} "  # noqa: S608
                    "WHERE tenant_id = :tid AND voided_at IS NULL "
                    "AND payment_method IS NOT NULL GROUP BY payment_method"
                ),
                {"tid": tid},
            )
        ).all()
        for raw, n in rows:
            if raw in _PAYMENT_CODES:
                continue
            code = normalize_payment_method(raw)
            print(f"    pago ({label}) {raw!r:28} → {code:12} ×{n}")
            if not apply:
                continue
            if code == "other":
                # Preservar el texto original: sin él la fila sería irrecuperable.
                await session.execute(
                    text(
                        f"UPDATE {tbl} SET payment_method = :code, "  # noqa: S608
                        "custom_fields = COALESCE(custom_fields, '{}'::jsonb) || "
                        "jsonb_build_object('payment_method_label', CAST(:raw AS TEXT)) "
                        "WHERE tenant_id = :tid AND voided_at IS NULL "
                        "AND payment_method = :raw"
                    ),
                    {"code": code, "raw": raw, "tid": tid},
                )
            else:
                await session.execute(
                    text(
                        f"UPDATE {tbl} SET payment_method = :code "  # noqa: S608
                        "WHERE tenant_id = :tid AND voided_at IS NULL "
                        "AND payment_method = :raw"
                    ),
                    {"code": code, "raw": raw, "tid": tid},
                )


async def backfill_products(session: AsyncSession, tid: uuid.UUID, apply: bool) -> None:
    vertical = (
        await session.execute(
            text("SELECT vertical_code FROM business_profiles WHERE tenant_id = :tid"),
            {"tid": tid},
        )
    ).scalar_one_or_none()
    canonical = set(PRODUCT_CATEGORY_LABELS.get(vertical or "kiosco_almacen",
                                                PRODUCT_CATEGORY_LABELS["kiosco_almacen"]))
    rows = (
        await session.execute(
            text(
                "SELECT id, category FROM products "
                "WHERE tenant_id = :tid AND category IS NOT NULL AND category != ''"
            ),
            {"tid": tid},
        )
    ).all()
    plan: Counter[tuple[str, str]] = Counter()
    for row_id, category in rows:
        if category in canonical:
            continue
        code, label = normalize_product_category(category, vertical)
        plan[(category, code)] += 1
        if apply:
            await session.execute(
                text(
                    "UPDATE products SET category = :code, "
                    "custom_fields = COALESCE(custom_fields, '{}'::jsonb) || "
                    "CASE WHEN :label IS NOT NULL THEN "
                    "jsonb_build_object('category_label', CAST(:label AS TEXT)) "
                    "ELSE '{}'::jsonb END "
                    "WHERE id = :id"
                ),
                {"code": code, "label": label or category, "id": row_id},
            )
    for (raw, code), n in sorted(plan.items()):
        print(f"    producto {raw!r:38} → {code:24} ×{n}")


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Escribir cambios (default: dry-run)")
    parser.add_argument("--tenant", help="UUID de tenant puntual")
    parser.add_argument("--all", action="store_true", help="Todos los tenants")
    parser.add_argument("--products", action="store_true", help="Incluir products.category")
    args = parser.parse_args()

    if args.apply and not (args.tenant or args.all):
        print("ERROR: --apply requiere --tenant <uuid> o --all explícito.")
        sys.exit(2)

    url, connect_args = async_engine_config()
    engine = create_async_engine(url, connect_args=connect_args)
    async with AsyncSession(engine) as session:
        tids = await _tenant_ids(session, args.tenant)
        mode = "APPLY" if args.apply else "DRY-RUN"
        print(f"[{mode}] {len(tids)} tenant(s)")
        for tid in tids:
            print(f"\n  tenant {tid}")
            await backfill_expenses(session, tid, args.apply)
            await backfill_payment_methods(session, tid, args.apply)
            if args.products:
                await backfill_products(session, tid, args.apply)
        if args.apply:
            await session.commit()
            print("\nCOMMIT aplicado.")
        else:
            await session.rollback()
            print("\nDry-run: nada se escribió.")
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
