"""Repara los productos cuyo costo unitario se cargó con el TOTAL del lote.

CAUSA (corregida en `column_mapping_service`, regla `"costo"`): una columna
«costo_total» se leía como `unit_cost_ars`. El producto nacía por el catálogo con
`stock_units = Cantidad` y un movimiento `catalog_initial_stock` cuyo `unit_cost` era
el TOTAL (480.000 por 40 unidades en vez de 12.000 cada una): el valor de inventario
se multiplicaba por la cantidad.

EVIDENCIA (estricta; nada se corrige por «se ve alto»): un producto se repara sólo si
1. el `confirm` de `pipeline_events` de SU archivo guardó el mapeo «total → unit_cost_ars»;
2. su único movimiento con costo es el `catalog_initial_stock` de ESE archivo (sin otra
   compra ni ajuste con costo, anulados aparte);
3. su costo vigente coincide con el del movimiento (nadie lo editó después); y
4. el movimiento no fue ya reparado (idempotente: consulta `decision_audit_log`); y
5. total / cantidad es exacto al centavo. Si no lo es, NO se toca: queda en la lista
   «a mano».

QUÉ ESCRIBE (con `--apply`, en UNA transacción, auditado): `inventory_movements.unit_cost`,
`products.unit_cost_ars` y `_vektor_costo_catalogo.costo` del producto, y marca el costo
promedio como pendiente. DESPUÉS correr el recálculo del historial y de las ventas:
    .venv/bin/python scripts/backfill_costo_promedio.py --tenant-id <uuid> --apply

REVERSIBLE: cada cambio queda en `decision_audit_log` (`COSTO_TOTAL_LOTE_REPAIR`) con
`before`/`after` del movimiento y del producto.

Uso (la DATABASE_URL la provee el usuario desde su shell; NUNCA se imprime):
    export DATABASE_URL='postgresql://...'
    .venv/bin/python scripts/reparar_costo_total_lote.py --tenant <uuid>           # dry-run
    .venv/bin/python scripts/reparar_costo_total_lote.py --tenant <uuid> \\
        --apply --ids <product_id>,<product_id>                                    # escribe

``--apply`` SIN ``--ids`` no corre. La evidencia del script (confirm con mapeo malo, único
movimiento con costo, división exacta) NO alcanza sola: un producto cargado bien desde el
mismo archivo cumple lo mismo y se dañaría (visto en la prueba del 2026-11-10: cinco
costos unitarios correctos aparecían como «a reparar»). Por eso el dry-run imprime cada
`product_id` y las fechas de carga, y la persona aprueba la lista exacta.

Hacer BACKUP de Neon (branch o snapshot) antes de `--apply`.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import uuid
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _db import (  # noqa: E402
    _SQL_MARCAR_COSTO_PROMEDIO,
    async_engine_config,
    insert_decision_audit,
)
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine  # noqa: E402

DECISION_TYPE = "COSTO_TOTAL_LOTE_REPAIR"
_CENTAVO = Decimal("0.01")

_SQL_ARCHIVOS = text(
    "SELECT file_id, max(created_at) AS confirmado_en FROM pipeline_events "
    "WHERE tenant_id = :t AND stage = 'confirm' AND file_id IS NOT NULL "
    "AND detail::text ~* '\"[^\"]*total[^\"]*\": *\"unit_cost_ars\"' "
    "GROUP BY file_id"
)
# Un solo movimiento con costo y vivo por producto: si hubo otra compra o ajuste con
# costo, el costo vigente ya no depende sólo de este movimiento y no se adivina.
_SQL_CANDIDATOS = text(
    "SELECT m.id AS movement_id, m.product_id, p.name, m.qty, m.unit_cost AS mov_cost, "
    "p.unit_cost_ars, m.source_upload_id, m.created_at AS cargado_en "
    "FROM inventory_movements m "
    "JOIN products p ON p.id = m.product_id AND p.tenant_id = m.tenant_id "
    "WHERE m.tenant_id = :t AND m.source_type = 'catalog_initial_stock' "
    "AND m.movement_type = 'adjustment' AND m.voided_at IS NULL AND m.qty > 1 "
    "AND m.source_upload_id = ANY(CAST(:files AS uuid[])) AND p.is_active "
    "AND NOT EXISTS ("
    "  SELECT 1 FROM inventory_movements o WHERE o.tenant_id = m.tenant_id "
    "  AND o.product_id = m.product_id AND o.id <> m.id AND o.voided_at IS NULL "
    "  AND o.unit_cost IS NOT NULL) "
    # Idempotente: un movimiento ya reparado queda con costo/cantidad que dividen
    # exacto otra vez (12.000 / 40 = 300): sin esto, una segunda corrida lo destruye.
    "AND NOT EXISTS ("
    "  SELECT 1 FROM decision_audit_log a WHERE a.tenant_id = m.tenant_id "
    "  AND a.decision_type = 'COSTO_TOTAL_LOTE_REPAIR' "
    "  AND a.decision_data->>'movement_id' = CAST(m.id AS text)) "
    "ORDER BY p.name"
)
_SQL_VALOR = text(
    "SELECT COALESCE(SUM(stock_units * unit_cost_ars / NULLIF(base_units_per_sale_unit, 0)), 0) "
    "FROM products WHERE tenant_id = :t AND is_active AND unit_cost_ars IS NOT NULL"
)


def proponer_unitario(total: Decimal, qty: int) -> Decimal | None:
    """total / qty si es EXACTO al centavo; si no, None (no se adivina)."""
    if qty <= 1 or total <= 0:
        return None
    unitario = (total / qty).quantize(_CENTAVO, rounding=ROUND_HALF_UP)
    return unitario if (unitario * qty).quantize(_CENTAVO) == total.quantize(_CENTAVO) else None


def _como_texto(valor: Decimal) -> str:
    """Como lo guarda el catálogo: «12000» (sin ceros ni notación científica)."""
    entero = valor == valor.to_integral_value()
    return format(valor.quantize(Decimal(1)) if entero else valor, "f")


def planificar(
    candidatos: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(a reparar, a mano). A reparar: costo vigente == costo del movimiento y división exacta."""
    reparar: list[dict[str, Any]] = []
    a_mano: list[dict[str, Any]] = []
    for c in candidatos:
        mov = Decimal(str(c["mov_cost"]))
        vigente = Decimal(str(c["unit_cost_ars"])) if c["unit_cost_ars"] is not None else None
        unitario = proponer_unitario(mov, int(c["qty"]))
        if vigente is None or vigente != mov:
            motivo = "el costo del producto ya no coincide con el del movimiento"
            a_mano.append({**c, "motivo": motivo})
        elif unitario is None:
            a_mano.append({**c, "motivo": "total/cantidad no es exacto al centavo"})
        else:
            reparar.append({**c, "unitario": unitario})
    return reparar, a_mano


def filtrar_por_ids(
    reparar: list[dict[str, Any]], ids: set[uuid.UUID]
) -> list[dict[str, Any]]:
    """Sólo los productos aprobados a mano. Un id que no es candidato aborta: nadie
    aprueba algo que el dry-run no propuso."""
    propuestos = {uuid.UUID(str(r["product_id"])) for r in reparar}
    desconocidos = ids - propuestos
    if desconocidos:
        raise SystemExit(
            "ids que NO están entre los candidatos a reparar: "
            + ", ".join(sorted(str(i) for i in desconocidos))
        )
    return [r for r in reparar if uuid.UUID(str(r["product_id"])) in ids]


async def _cargar(conn: AsyncConnection, tenant_id: uuid.UUID) -> list[dict[str, Any]]:
    confirms = (await conn.execute(_SQL_ARCHIVOS, {"t": tenant_id})).all()
    archivos = {r.file_id: r.confirmado_en for r in confirms}
    if not archivos:
        return []
    filas = (
        await conn.execute(
            _SQL_CANDIDATOS, {"t": tenant_id, "files": [str(a) for a in archivos]}
        )
    ).mappings().all()
    return [{**dict(f), "confirmado_en": archivos.get(f["source_upload_id"])} for f in filas]


async def _aplicar(
    conn: AsyncConnection, tenant_id: uuid.UUID, reparar: list[dict[str, Any]]
) -> int:
    hechos = 0
    for r in reparar:
        antes_mov, antes_prod, nuevo = r["mov_cost"], r["unit_cost_ars"], r["unitario"]
        # Guardas: sólo escribe si el valor sigue siendo el que se auditó en el dry-run.
        res_mov = await conn.execute(
            text(
                "UPDATE inventory_movements SET unit_cost = :n "
                "WHERE id = :mid AND tenant_id = :t AND unit_cost = :antes"
            ),
            {"n": nuevo, "mid": r["movement_id"], "t": tenant_id, "antes": antes_mov},
        )
        res_prod = await conn.execute(
            text(
                "UPDATE products SET unit_cost_ars = :n, custom_fields = CASE "
                "WHEN jsonb_exists(COALESCE(custom_fields, '{}'::jsonb), '_vektor_costo_catalogo') "
                "THEN jsonb_set(custom_fields, '{_vektor_costo_catalogo,costo}', "
                "to_jsonb(CAST(:ntxt AS text)), false) ELSE custom_fields END "
                "WHERE id = :pid AND tenant_id = :t AND unit_cost_ars = :antes"
            ),
            {
                "n": nuevo,
                "ntxt": _como_texto(nuevo),
                "pid": r["product_id"],
                "t": tenant_id,
                "antes": antes_prod,
            },
        )
        if res_mov.rowcount != 1 or res_prod.rowcount != 1:
            raise RuntimeError(
                f"{r['name']}: el dato cambió desde el dry-run; se aborta TODO (rollback)."
            )
        await conn.execute(
            text(_SQL_MARCAR_COSTO_PROMEDIO.format(mid=":mid")), {"mid": r["movement_id"]}
        )
        await insert_decision_audit(
            conn,
            tenant_id=str(tenant_id),
            decision_type=DECISION_TYPE,
            decision_data={
                "movement_id": str(r["movement_id"]),
                "product_id": str(r["product_id"]),
                "name": r["name"],
                "qty": int(r["qty"]),
                "before": {
                    "movement_unit_cost": str(antes_mov),
                    "product_unit_cost": str(antes_prod),
                },
                "after": {
                    "movement_unit_cost": str(nuevo),
                    "product_unit_cost": str(nuevo),
                },
            },
            triggered_by="script:reparar_costo_total_lote",
        )
        hechos += 1
    return hechos


async def main(tenant_id: uuid.UUID, apply: bool, ids: set[uuid.UUID]) -> None:
    url, connect_args = async_engine_config()
    engine = create_async_engine(url, connect_args=connect_args)
    try:
        async with engine.begin() as conn:
            valor_antes = (await conn.execute(_SQL_VALOR, {"t": tenant_id})).scalar_one()
            reparar, a_mano = planificar(await _cargar(conn, tenant_id))
            print(f"valor de inventario ANTES: {valor_antes}")
            print(f"\nA REPARAR ({len(reparar)}):")
            for r in reparar:
                print(
                    f"  {r['product_id']} | {r['name'][:28]:28} qty={r['qty']:>4} "
                    f"total={r['mov_cost']} -> unitario={r['unitario']}\n"
                    f"      movimiento cargado={r['cargado_en']}  "
                    f"archivo confirmado={r['confirmado_en']}"
                )
            print(f"\nA MANO ({len(a_mano)}):")
            for r in a_mano:
                print(
                    f"  {r['name'][:32]:32} qty={r['qty']:>4} costo={r['mov_cost']}: {r['motivo']}"
                )
            if not apply:
                print(
                    "\nDRY-RUN: no se escribió nada. Revisá la lista y, para aplicar, pasá "
                    "--apply --ids <uuid>,<uuid>… con SOLO los que son el bug."
                )
                return
            if not ids:
                raise SystemExit(
                    "--apply exige --ids con los productos aprobados a mano: la evidencia del "
                    "script no alcanza para distinguir un total de un costo unitario correcto."
                )
            hechos = await _aplicar(conn, tenant_id, filtrar_por_ids(reparar, ids))
            valor_despues = (await conn.execute(_SQL_VALOR, {"t": tenant_id})).scalar_one()
            print(f"\nAPLICADO: {hechos} producto(s). valor de inventario DESPUÉS: {valor_despues}")
            if hechos:
                print(
                    "Ahora corré: .venv/bin/python scripts/backfill_costo_promedio.py "
                    f"--tenant-id {tenant_id} --apply"
                )
    finally:
        await engine.dispose()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--tenant", required=True, type=uuid.UUID)
    ap.add_argument("--apply", action="store_true", help="escribe (default: dry-run)")
    ap.add_argument(
        "--ids",
        default="",
        help="product_id aprobados, separados por coma (obligatorio con --apply)",
    )
    args = ap.parse_args()
    aprobados = {uuid.UUID(x.strip()) for x in args.ids.split(",") if x.strip()}
    asyncio.run(main(args.tenant, args.apply, aprobados))
