"""Corrige el signo de movimientos 'adjustment' puntuales cuya qty negativa se sabe
que está mal cargada (debería ser una recepción/alta de stock, no una pérdida).

Para cada movimiento encontrado (por tenant + nombre de producto, vivo, qty < 0):
  - Flipea qty a positivo (registro histórico correcto: fue un alta, no una baja).
  - Ajusta products.stock_units / inventory_balances.current_qty en +2*|qty|: una vez
    para deshacer el efecto (resta) que el signo malo ya aplicó, y otra para aplicar el
    efecto correcto (suma) que debería haber tenido. Clamp a >= 0 (mismo criterio que
    repair_inventory_ledger.py).

Dry-run por defecto, auditado (decision_audit_log, decision_type=ADJUSTMENT_SIGN_FIX),
reversible (revertir: recrear con qty negativa + restar 2*|qty| del stock).

Usage:
    DATABASE_URL='...' .venv/bin/python scripts/fix_adjustment_sign.py \
        --tenant <uuid> --product "Coca Cola 1.5L" --product "Agua Villavicencio 1.5L" \
        --out preview.csv
    ... --apply
"""

import argparse
import asyncio
import csv
import uuid

from _db import async_engine_config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

_DECISION_TYPE = "ADJUSTMENT_SIGN_FIX"


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--product", required=True, action="append", dest="products")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--out", default="adjustment_sign_fix.csv")
    args = parser.parse_args()

    tid = uuid.UUID(args.tenant)
    url, connect_args = async_engine_config()
    engine = create_async_engine(url, connect_args=connect_args)
    async with AsyncSession(engine) as session:
        rows = (
            await session.execute(
                text(
                    "SELECT im.id, p.id AS product_id, p.name, p.stock_units, im.qty, "
                    "       im.created_at "
                    "FROM inventory_movements im "
                    "JOIN products p ON p.id = im.product_id "
                    "WHERE im.tenant_id = :tid AND im.voided_at IS NULL "
                    "AND im.movement_type = 'adjustment' AND im.qty < 0 "
                    "AND p.name = ANY(:names)"
                ),
                {"tid": tid, "names": args.products},
            )
        ).mappings().all()

        print(f"{len(rows)} movimiento(s) encontrados:\n")
        report_rows = []
        for r in rows:
            old_qty = int(r["qty"])
            stock_before = int(r["stock_units"])
            stock_after = stock_before + 2 * abs(old_qty)
            print(
                f"  {r['name']}: qty {old_qty} → {abs(old_qty)} | "
                f"stock_units {stock_before} → {stock_after}"
            )
            report_rows.append(
                {
                    "movement_id": str(r["id"]),
                    "product_id": str(r["product_id"]),
                    "product_name": r["name"],
                    "old_qty": old_qty,
                    "new_qty": abs(old_qty),
                    "stock_before": stock_before,
                    "stock_after": stock_after,
                    "created_at": str(r["created_at"]),
                }
            )

        if report_rows:
            with open(args.out, "w", newline="", encoding="utf-8") as fh:
                writer = csv.DictWriter(fh, fieldnames=list(report_rows[0].keys()))
                writer.writeheader()
                writer.writerows(report_rows)
            print(f"\nReporte escrito en {args.out}.")

        if args.apply and report_rows:
            for r in report_rows:
                await session.execute(
                    text(
                        "UPDATE inventory_movements SET qty = :new_qty "
                        "WHERE id = CAST(:mid AS uuid) AND tenant_id = :tid"
                    ),
                    {"new_qty": r["new_qty"], "mid": r["movement_id"], "tid": tid},
                )
                delta = 2 * abs(r["old_qty"])
                await session.execute(
                    text(
                        "UPDATE products SET stock_units = GREATEST(0, stock_units + :dq) "
                        "WHERE tenant_id = :tid AND id = CAST(:pid AS uuid)"
                    ),
                    {"tid": tid, "pid": r["product_id"], "dq": delta},
                )
                await session.execute(
                    text(
                        "UPDATE inventory_balances "
                        "SET current_qty = GREATEST(0, current_qty + :dq) "
                        "WHERE tenant_id = :tid AND product_id = CAST(:pid AS uuid)"
                    ),
                    {"tid": tid, "pid": r["product_id"], "dq": delta},
                )
            await session.execute(
                text(
                    "INSERT INTO decision_audit_log "
                    "(id, tenant_id, decision_type, decision_data, created_at) "
                    "VALUES (gen_random_uuid(), :tid, :dtype, CAST(:data AS jsonb), now())"
                ),
                {
                    "tid": tid,
                    "dtype": _DECISION_TYPE,
                    "data": __import__("json").dumps({"fixed": report_rows}),
                },
            )
            await session.commit()
            print(f"\nCOMMIT: {len(report_rows)} movimiento(s) corregido(s).")
        elif not args.apply:
            await session.rollback()
            print("\nDry-run: nada se escribió.")
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
