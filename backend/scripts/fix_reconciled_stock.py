"""Corrige stock_units de productos puntuales cuya reconciliación (stock inicial de
catálogo + compras del archivo fuente − ventas vivas) fue verificada A MANO contra
documentos del tenant y difiere de lo que muestra el sistema — caso real: tenant
"don pedro" (2026-07), donde 3 de 95 productos quedaron con stock inflado por
movimientos 'adjustment' sin `source_type` (ruido no reconciliable), mientras los
otros 92 ya estaban correctos.

IMPORTANTE — por qué "compras" es un parámetro y NO se re-consulta en vivo: se
verificó que las corridas de `repair_inventory_ledger.py` de hoy (2026-07-03)
voidearon compras VÁLIDAS de estos 3 productos. Causa raíz: `inventory_movements.
created_at` quedó con la fecha de CARGA del archivo (2026-06-19), no la fecha real
de cada compra — el dedup agrupa por `date(created_at)+qty+costo`, y dos compras
reales de meses distintos con la misma cantidad (packs de proveedor de tamaño fijo,
ej. 36 unidades) quedaron indistinguibles de un duplicado real bajo esa fecha de
carga compartida, y se voidearon de más. El total vivo en la DB para 'purchase' NO
es confiable para estos productos; el total del archivo fuente sí. (Este bug de
raíz — created_at de import en vez de fecha real rompiendo el supuesto del dedup —
queda pendiente como investigación aparte, potencialmente afecta más productos.)

Para cada producto (--product "Nombre:stock_inicial_catalogo:compras_verificadas"):
  1. Busca el producto por nombre exacto (case-sensitive) dentro del tenant.
  2. Compras: el valor pasado por parámetro (verificado a mano contra el archivo
     fuente del tenant, NO se re-consulta el ledger — ver nota de arriba).
  3. Ventas vivas: SUM(quantity) de sales_entries WHERE voided_at IS NULL (sí se
     consulta en vivo — sin evidencia de que sales_entries tenga el mismo problema).
  4. stock_esperado = max(0, stock_inicial_catalogo + compras − ventas_vivas).
  5. Ajustes 'adjustment' vivos SIN source_type de este producto: se VOIDAN (son el
     ruido sin procedencia que infló el stock — confirmado por la reconciliación).
  6. Se fija products.stock_units = stock_esperado (SET absoluto, no incremental —
     ya se calculó el valor correcto completo). Se sincroniza
     inventory_balances.current_qty igual. NO toca los movimientos 'purchase'
     voideados de más — esa reparación del ledger queda pendiente aparte.

Todo dry-run por defecto; --apply escribe. Auditado en decision_audit_log
(decision_type='INVENTORY_RECONCILIATION_FIX'). SOLO SELECT en dry-run. NUNCA
imprime la connection URL.

Usage:
    # Dry-run (default) — revisar antes de aplicar
    DATABASE_URL='postgresql://...neon.tech/vektor?sslmode=require' \
        .venv/bin/python scripts/fix_reconciled_stock.py \
        --tenant ee2625dc-96b7-464c-bda3-7f7018cc2a5b \
        --product "Coca Cola 1.5L:36:217" \
        --product "Agua Villavicencio 1.5L:36:258" \
        --product "Gomitas Trulala x100g:24:228"

    # Aplicar (agregar --apply al mismo comando)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _db import async_engine_config  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine  # noqa: E402

_DECISION_TYPE = "INVENTORY_RECONCILIATION_FIX"
_TRIGGERED_BY = "script:fix_reconciled_stock"


def _parse_product_arg(raw: str) -> tuple[str, int, int]:
    parts = raw.rsplit(":", 2)
    if len(parts) != 3 or not all(p.lstrip("-").isdigit() for p in parts[1:]):
        raise argparse.ArgumentTypeError(
            f"Formato inválido {raw!r} — esperado "
            "'Nombre del producto:stock_inicial:compras_verificadas'"
        )
    name, inicial, compras = parts
    if not name:
        raise argparse.ArgumentTypeError(f"Formato inválido {raw!r} — falta el nombre")
    return name, int(inicial), int(compras)


async def _plan_product(
    session: AsyncSession, tid: uuid.UUID, name: str, inicial: int, compras: int
) -> dict[str, Any] | None:
    prod = (
        await session.execute(
            text("SELECT id, stock_units FROM products WHERE tenant_id = :tid AND name = :n"),
            {"tid": tid, "n": name},
        )
    ).mappings().first()
    if prod is None:
        print(f"  ⚠ {name!r}: NO ENCONTRADO en este tenant — se saltea.")
        return None
    pid, stock_units = prod["id"], int(prod["stock_units"])

    ventas = (
        await session.execute(
            text(
                "SELECT COALESCE(SUM(quantity), 0) FROM sales_entries "
                "WHERE tenant_id = :tid AND product_id = :pid AND voided_at IS NULL"
            ),
            {"tid": tid, "pid": pid},
        )
    ).scalar_one()
    stock_esperado = max(0, inicial + int(compras) - int(ventas))

    disputed = (
        await session.execute(
            text(
                "SELECT id, qty FROM inventory_movements "
                "WHERE tenant_id = :tid AND product_id = :pid "
                "AND movement_type = 'adjustment' AND voided_at IS NULL "
                "AND source_type IS NULL"
            ),
            {"tid": tid, "pid": pid},
        )
    ).mappings().all()

    return {
        "product_id": str(pid),
        "name": name,
        "stock_units": stock_units,
        "inicial": inicial,
        "compras": int(compras),
        "ventas": int(ventas),
        "stock_esperado": stock_esperado,
        "diff": stock_units - stock_esperado,
        "disputed_movement_ids": [str(r["id"]) for r in disputed],
        "disputed_sum": sum(int(r["qty"]) for r in disputed),
    }


async def _apply_product(session: AsyncSession, tid: uuid.UUID, plan: dict[str, Any]) -> None:
    pid = plan["product_id"]
    for mid in plan["disputed_movement_ids"]:
        await session.execute(
            text(
                "UPDATE inventory_movements SET voided_at = now() "
                "WHERE tenant_id = :tid AND id = CAST(:mid AS uuid) AND voided_at IS NULL"
            ),
            {"tid": tid, "mid": mid},
        )
    await session.execute(
        text(
            "UPDATE products SET stock_units = :sq "
            "WHERE tenant_id = :tid AND id = CAST(:pid AS uuid)"
        ),
        {"tid": tid, "pid": pid, "sq": plan["stock_esperado"]},
    )
    existing_balance = (
        await session.execute(
            text(
                "SELECT id FROM inventory_balances "
                "WHERE tenant_id = :tid AND product_id = CAST(:pid AS uuid)"
            ),
            {"tid": tid, "pid": pid},
        )
    ).first()
    if existing_balance is not None:
        await session.execute(
            text(
                "UPDATE inventory_balances SET current_qty = :sq "
                "WHERE tenant_id = :tid AND product_id = CAST(:pid AS uuid)"
            ),
            {"tid": tid, "pid": pid, "sq": plan["stock_esperado"]},
        )
    else:
        await session.execute(
            text(
                "INSERT INTO inventory_balances "
                "(id, tenant_id, product_id, current_qty, reserved_qty, created_at, updated_at) "
                "VALUES (gen_random_uuid(), :tid, CAST(:pid AS uuid), :sq, 0, now(), now())"
            ),
            {"tid": tid, "pid": pid, "sq": plan["stock_esperado"]},
        )
    await session.execute(
        text(
            "INSERT INTO decision_audit_log "
            "(id, tenant_id, decision_type, decision_data, triggered_by, created_at) "
            "VALUES (gen_random_uuid(), :tid, :dt, CAST(:dd AS jsonb), :tb, now())"
        ),
        {
            "tid": tid,
            "dt": _DECISION_TYPE,
            "dd": json.dumps(plan),
            "tb": _TRIGGERED_BY,
        },
    )


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Corrige stock_units de productos reconciliados a mano contra archivos fuente."
    )
    parser.add_argument("--tenant", required=True, help="UUID del tenant")
    parser.add_argument(
        "--product",
        required=True,
        action="append",
        dest="products",
        type=_parse_product_arg,
        help="'Nombre del producto:stock_inicial:compras_verificadas' (repetible)",
    )
    parser.add_argument("--apply", action="store_true", help="Escribir cambios (default: dry-run)")
    args = parser.parse_args()

    tid = uuid.UUID(args.tenant)
    url, connect_args = async_engine_config()
    engine = create_async_engine(url, connect_args=connect_args)
    async with AsyncSession(engine) as session:
        mode = "APPLY" if args.apply else "DRY-RUN"
        print(f"[{mode}] corrección de stock reconciliado — tenant {tid}\n")

        plans: list[dict[str, Any]] = []
        for name, inicial, compras in args.products:
            plan = await _plan_product(session, tid, name, inicial, compras)
            if plan is None:
                continue
            plans.append(plan)
            print(f"  {plan['name']!r}:")
            print(
                f"    stock_units actual = {plan['stock_units']}   "
                f"esperado = {plan['stock_esperado']}   diff = {plan['diff']}"
            )
            print(
                f"    inicial(catálogo)={plan['inicial']}  "
                f"compras(archivo, verificado)={plan['compras']}  ventas_vivas={plan['ventas']}"
            )
            print(
                f"    adjustment sin source_type a voidar: "
                f"{len(plan['disputed_movement_ids'])} fila(s), Σqty={plan['disputed_sum']}"
            )
            if plan["diff"] == 0:
                print("    (sin diferencia — no hace falta corregir)")
            print()

        if args.apply:
            to_apply = [p for p in plans if p["diff"] != 0]
            for plan in to_apply:
                await _apply_product(session, tid, plan)
            await session.commit()
            print(
                f"COMMIT: {len(to_apply)} producto(s) corregido(s). "
                f"decision_type={_DECISION_TYPE}."
            )
        else:
            await session.rollback()
            print("Dry-run: nada se escribió. Revisá los números antes de correr con --apply.")
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
