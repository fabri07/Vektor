"""Read-only: por qué las compras VIVAS en la DB de un producto son menos que en el
archivo fuente del tenant. Compara vivas vs voideadas vs total, y si hay
voideadas, busca qué decision_audit_log (INVENTORY_REPAIR) las anuló.

Usage:
    DATABASE_URL='postgresql://...neon.tech/vektor?sslmode=require' \
        .venv/bin/python scripts/diag_missing_purchases_scope.py \
        --tenant ee2625dc-96b7-464c-bda3-7f7018cc2a5b \
        --product "Coca Cola 1.5L" --product "Agua Villavicencio 1.5L" \
        --product "Gomitas Trulala x100g"

ONLY runs SELECT statements. No writes. Safe against production.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _db import async_engine_config  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine  # noqa: E402


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--product", required=True, action="append", dest="products")
    args = parser.parse_args()
    tid = uuid.UUID(args.tenant)

    url, connect_args = async_engine_config()
    engine = create_async_engine(url, connect_args=connect_args)
    async with AsyncSession(engine) as session:
        # Traído una sola vez: todas las corridas de reparación del tenant. Se
        # filtra en Python (evita depender de operadores de array de Postgres
        # para un script de un solo uso).
        all_repair_audits = (
            await session.execute(
                text(
                    "SELECT id, decision_type, triggered_by, created_at, decision_data "
                    "FROM decision_audit_log "
                    "WHERE tenant_id = :tid AND decision_type IN "
                    "('INVENTORY_REPAIR', 'INVENTORY_RECONCILIATION_FIX') "
                    "ORDER BY created_at"
                ),
                {"tid": tid},
            )
        ).mappings().all()
        for name in args.products:
            prod = (
                await session.execute(
                    text("SELECT id FROM products WHERE tenant_id = :tid AND name = :n"),
                    {"tid": tid, "n": name},
                )
            ).first()
            if prod is None:
                print(f"\n=== {name}: NO ENCONTRADO ===")
                continue
            pid = prod[0]
            print(f"\n=== {name} ===")

            summary = (
                await session.execute(
                    text(
                        "SELECT "
                        "COUNT(*) FILTER (WHERE voided_at IS NULL) AS n_live, "
                        "COALESCE(SUM(qty) FILTER (WHERE voided_at IS NULL), 0) AS sum_live, "
                        "COUNT(*) FILTER (WHERE voided_at IS NOT NULL) AS n_voided, "
                        "COALESCE(SUM(qty) FILTER (WHERE voided_at IS NOT NULL), 0) AS sum_voided "
                        "FROM inventory_movements "
                        "WHERE tenant_id = :tid AND product_id = :pid "
                        "AND movement_type = 'purchase'"
                    ),
                    {"tid": tid, "pid": pid},
                )
            ).mappings().one()
            print(
                f"  vivas: n={summary['n_live']} Σ={summary['sum_live']}   "
                f"voideadas: n={summary['n_voided']} Σ={summary['sum_voided']}"
            )

            voided_rows = (
                await session.execute(
                    text(
                        "SELECT id, qty, created_at, voided_at, source_row_hash, "
                        "source_upload_id "
                        "FROM inventory_movements "
                        "WHERE tenant_id = :tid AND product_id = :pid "
                        "AND movement_type = 'purchase' AND voided_at IS NOT NULL "
                        "ORDER BY created_at"
                    ),
                    {"tid": tid, "pid": pid},
                )
            ).mappings().all()
            if voided_rows:
                print(f"  {len(voided_rows)} compra(s) voideada(s) — detalle:")
                voided_ids = []
                for r in voided_rows:
                    print(
                        f"    id={r['id']} qty={r['qty']} created_at={r['created_at']} "
                        f"voided_at={r['voided_at']} hash={r['source_row_hash']}"
                    )
                    voided_ids.append(str(r["id"]))

                # Buscar qué corrida de reparación las anuló, filtrando en Python.
                audits = [
                    a
                    for a in all_repair_audits
                    if any(
                        vid
                        in (
                            a["decision_data"]
                            if isinstance(a["decision_data"], str)
                            else json.dumps(a["decision_data"])
                        )
                        for vid in voided_ids
                    )
                ]
                if audits:
                    print(f"  Referenciadas en {len(audits)} decision_audit_log:")
                    for a in audits:
                        dd = a["decision_data"]
                        dd_dict = json.loads(dd) if isinstance(dd, str) else dd
                        print(
                            f"    [{a['created_at']}] decision_type={a['decision_type']} "
                            f"triggered_by={a['triggered_by']} "
                            f"reason={dd_dict.get('reason') or dd_dict.get('by_reason')}"
                        )
                else:
                    print("  ⚠ Ninguna decision_audit_log las referencia — sin auditoría.")
            else:
                print("  Ninguna compra voideada — el faltante NO es por void, nunca se cargaron.")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
