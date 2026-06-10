"""Soft-delete auditado de registros basura de imports mal clasificados.

Usage:
    # Dry-run (default): lista qué anularía, no escribe nada.
    DATABASE_URL='postgresql://...' .venv/bin/python scripts/void_misclassified_imports.py \
        --tenant <uuid> --target expenses --category importado

    # Ventas falsas que duplican gastos (misma fecha + mismo monto):
    ... --tenant <uuid> --target sales --match-expense-category importado

    # Por día de import:
    ... --tenant <uuid> --target sales --created-on 2026-06-06

    # Aplicar:
    ... --apply

Filtros (se combinan con AND; al menos uno es obligatorio):
  --created-on YYYY-MM-DD        created_at::date del registro (día del import)
  --category X                   (expenses) category exacta
  --match-expense-category X     (sales) existe un gasto NO anulado del tenant con
                                 misma fecha calendario, mismo monto y esa categoría
  --notes-like 'patrón'          notes (sales) / description (expenses) ILIKE

Anula con voided_at=now() + void_reason=REPAIR_MISCLASSIFIED_IMPORT (reversible:
UPDATE ... SET voided_at=NULL, void_reason=NULL). NUNCA borra filas ni imprime
la connection URL. Requiere correr desde backend/.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import uuid
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _db import async_engine_config  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine  # noqa: E402

_VOID_REASON = "REPAIR_MISCLASSIFIED_IMPORT"


def _build_where(args: argparse.Namespace, params: dict[str, object]) -> tuple[str, str]:
    """Devuelve (tabla, cláusula WHERE) con los filtros combinados por AND."""
    table = "sales_entries" if args.target == "sales" else "expense_entries"
    clauses = ["tenant_id = :tid", "voided_at IS NULL"]
    if args.created_on:
        clauses.append("created_at::date = :created_on")
        params["created_on"] = date.fromisoformat(args.created_on)
    if args.category:
        if args.target != "expenses":
            print("ERROR: --category aplica solo a --target expenses.")
            sys.exit(2)
        clauses.append("category = :category")
        params["category"] = args.category
    if args.match_expense_category:
        if args.target != "sales":
            print("ERROR: --match-expense-category aplica solo a --target sales.")
            sys.exit(2)
        clauses.append(
            "EXISTS (SELECT 1 FROM expense_entries e "
            "WHERE e.tenant_id = :tid AND e.voided_at IS NULL "
            "AND e.category = :mcat "
            "AND e.transaction_date::date = sales_entries.transaction_date::date "
            "AND e.amount = sales_entries.amount)"
        )
        params["mcat"] = args.match_expense_category
    if args.notes_like:
        col = "notes" if args.target == "sales" else "description"
        clauses.append(f"{col} ILIKE :notes_like")
        params["notes_like"] = args.notes_like
    if len(clauses) == 2:
        print("ERROR: indicá al menos un filtro (--created-on / --category / "
              "--match-expense-category / --notes-like). Anular todo un tenant "
              "a ciegas no está permitido.")
        sys.exit(2)
    return table, " AND ".join(clauses)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant", required=True, help="UUID del tenant (obligatorio)")
    parser.add_argument("--target", required=True, choices=("sales", "expenses"))
    parser.add_argument("--created-on", help="created_at::date == YYYY-MM-DD")
    parser.add_argument("--category", help="(expenses) category exacta")
    parser.add_argument(
        "--match-expense-category",
        help="(sales) anula ventas que duplican un gasto de esa categoría "
        "(misma fecha calendario + mismo monto)",
    )
    parser.add_argument("--notes-like", help="notes/description ILIKE patrón")
    parser.add_argument("--apply", action="store_true", help="Escribir (default: dry-run)")
    args = parser.parse_args()

    tid = uuid.UUID(args.tenant)
    params: dict[str, object] = {"tid": tid}
    table, where = _build_where(args, params)
    desc_col = "notes" if args.target == "sales" else "description"

    url, connect_args = async_engine_config()
    engine = create_async_engine(url, connect_args=connect_args)
    async with AsyncSession(engine) as session:
        rows = (
            await session.execute(
                text(
                    f"SELECT id, amount, transaction_date, {desc_col}, created_at "  # noqa: S608
                    f"FROM {table} WHERE {where} ORDER BY transaction_date"
                ),
                params,
            )
        ).all()
        total = sum(r[1] for r in rows)
        mode = "APPLY" if args.apply else "DRY-RUN"
        print(f"[{mode}] tenant {tid} — {args.target}: {len(rows)} registro(s), "
              f"total ${total}")
        for r in rows[:15]:
            print(f"    {r[2]}  ${r[1]:>12}  {str(r[3])[:50]!r}  (import {r[4]:%Y-%m-%d})")
        if len(rows) > 15:
            print(f"    ... y {len(rows) - 15} más")

        if args.apply and rows:
            result = await session.execute(
                text(
                    f"UPDATE {table} SET voided_at = now(), "  # noqa: S608
                    f"void_reason = '{_VOID_REASON}' WHERE {where}"
                ),
                params,
            )
            await session.commit()
            print(f"\nCOMMIT: {result.rowcount} registro(s) anulados "
                  f"(void_reason={_VOID_REASON}). Reversible con voided_at=NULL.")
        elif args.apply:
            print("\nNada para anular.")
        else:
            await session.rollback()
            print("\nDry-run: nada se escribió.")
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
