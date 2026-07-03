"""Revierte el colapso de marcas→sentinela hecho por deactivate_brand_suppliers.py.

Sistémico, reversible, dry-run por defecto. Deshace el daño de datos que dejó
`deactivate_brand_suppliers.py` (caminos MERCH_SOURCE_NO_CONTACT y BRAND_FROM_CATALOG):
las marcas de catálogo quedaron como PROVEEDORES INACTIVOS y sus compras se
re-apuntaron al sentinela "No identificado", dejando los productos colgados en ese
bucket anónimo. Este script reactiva esas marcas-proveedor y devuelve sus
compras/gastos a su origen, para que la mercadería vuelva a mostrarse bajo la marca.

FUENTE DE VERDAD: decision_audit_log (NO SQL a ciegas). Lee TODAS las filas que el
colapso dejó por proveedor (un proveedor puede haber sido colapsado por los dos
caminos) y las fusiona:
    SUPPLIER_MERCH_SOURCE_COLLAPSE  (decision_data: supplier_id, expense_ids,
        product_ids, collapsed_to_sentinel, ...) → re-apuntó gastos + movimientos al
        sentinela y movió la marca al producto.
    SUPPLIER_BRAND_CLEANUP          (decision_data: supplier_id, product_ids, ...) →
        soft-deleteó la marca-proveedor y quitó la marca del producto (guardada en
        products.custom_fields["_marca_prev"]). NO tocó gastos/movimientos.

QUÉ HACE POR CADA MARCA-PROVEEDOR (idempotente — si ya está revertido, saltea sin
escribir ni auditar):
  1. Reactiva la marca-proveedor SOLO si está dada de baja (deactivated_at IS NOT NULL)
     y la marca como PROVISIONAL: custom_fields["_provisional_from_brand"] = true. Es un
     proveedor provisional derivado de marca, NO un proveedor real validado (para que la
     UI lo distinga y se pueda reasignar luego a un distribuidor real). Si el proveedor
     YA está activo (p. ej. alguien lo validó a mano y le sacó el flag), NO se toca su
     estado ni su custom_fields.
  2. (MERCH) Devuelve los gastos a su proveedor original con ancla EXACTA: los que hoy
     apuntan al sentinela y tienen custom_fields["_supplier_prev"] == supplier_id.
     supplier_name se toma del name del proveedor reactivado (el texto previo no se
     preservó); supplier_id es la corrección autoritativa.
  3. (MERCH) Re-apunta los movimientos de compra sentinela → marca-proveedor SOLO con
     ALTA precisión. No existe vínculo directo gasto↔movimiento y el colapso NO guardó
     IDs de movimientos (guardó product_ids por gasto + movements_attributed total);
     así que un producto se re-apunta solo si:
       - lo reclama UNA sola marca-proveedor colapsada por MERCH en el audit (entre las
         que EXISTEN — un proveedor borrado o el sentinela no cuentan como claim), Y
       - no tiene además movimientos de compra hacia un proveedor real (≠ sentinela), Y
       - el movimiento es ANTERIOR al colapso (created_at <= fecha del audit): lo que el
         colapso movió existía antes de que se escribiera el audit; una compra anónima
         registrada después queda afuera por construcción (también cierra la ventana
         plan→apply contra escrituras concurrentes de la app), Y
       - a nivel proveedor, el total de movimientos candidatos NO supera el
         movements_attributed que el colapso registró (si hay de más, había compras
         sentinela previas al colapso mezcladas → no se adivina, todo el proveedor
         queda LOW).
     NOTA: NO se compara contra el conteo de product_ids del audit — eso cuenta GASTOS
     (uno por expense colapsado), no movimientos, y un producto puede tener más
     movimientos que gastos (unidades distintas).
     Todo lo dudoso NO se toca y se reporta como LOW_CONFIDENCE (revisión humana).
  4. Marca del producto: se restaura desde "_marca_prev" en los productos que el audit
     registró (devuelve la marca que BRAND había quitado y la marca previa distinta que
     MERCH había pisado); se limpian los flags "_marca_prev"/"_reassigned_from".
  5. Audita cada reversión: decision_type = 'SUPPLIER_COLLAPSE_REVERT'.

PRECONDICIONES: asume la migración 20260727_0001 aplicada (inventory_movements.voided_at)
y correr DESPUÉS de scripts/repair_inventory_ledger.py — los movimientos anulados por el
repair se ignoran en conteos y re-apuntes (voided_at IS NULL).

COMPUERTA DE EJECUCIÓN (apply por tandas — un apply masivo de datos históricos merece
revisión humana):
  1. Dry-run global:   scripts/revert_brand_supplier_collapse.py --all-active --out r.csv
  2. Revisar el reporte tenant por tenant (sobre todo los LOW_CONFIDENCE/pendientes).
  3. Apply a UN tenant conocido primero:  ... --tenant <uuid> --apply
  4. Recién después, global o por batches:  ... --all-active --apply

REVERSA DE ESTE SCRIPT (por si hiciera falta): la traza queda en decision_audit_log con
decision_type='SUPPLIER_COLLAPSE_REVERT' (supplier_id, expense_ids, movement_products,
sentinel). NUNCA borra filas ni imprime la URL. Correr desde backend/.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import sys
import uuid
from collections import Counter
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _db import async_engine_config  # noqa: E402
from sqlalchemy import bindparam, text  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine  # noqa: E402

# Sentinela: fuente de verdad en app/persistence/models/_sentinel.py. Se replica acá
# (mismo criterio) para mantener el script autocontenido y no disparar la cadena de
# imports del paquete `app` (config/ORM) — igual que deactivate_brand_suppliers.py.
SENTINEL_FLAG_KEY = "_sentinel"


def is_sentinel_value(value: object) -> bool:
    """True si el flag de sentinela representa "activo" (string "true" o bool True)."""
    return value in ("true", True)


_SENTINEL_NAME = "No identificado"
_PROVISIONAL_FLAG = "_provisional_from_brand"

# decision_types que dejó el colapso (fuente de verdad).
_MERCH_COLLAPSE = "SUPPLIER_MERCH_SOURCE_COLLAPSE"
_BRAND_COLLAPSE = "SUPPLIER_BRAND_CLEANUP"
# decision_type que emite ESTE script.
_REVERT_DECISION = "SUPPLIER_COLLAPSE_REVERT"

# Acciones posibles por candidato (van al reporte).
_ACT_REVERT = "REVERT"
_ACT_ALREADY_ACTIVE = "ALREADY_ACTIVE"  # sin trabajo pendiente que escribir
_ACT_SKIP_SENTINEL = "SKIP_SENTINEL"
_ACT_NOT_FOUND = "NOT_FOUND"  # proveedor borrado/inexistente


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        return json.loads(value)  # type: ignore[no-any-return]
    return value or {}


async def _load_collapses(session: AsyncSession, tid: uuid.UUID) -> list[dict[str, Any]]:
    """Colapsos del tenant fusionados por proveedor (ambos tipos). Read-only.

    Un proveedor pudo colapsarse por MERCH y por BRAND; se acumulan TODAS sus filas de
    audit: unión de product_ids, productos MERCH (anclaje de movimientos), fecha del
    último colapso MERCH (ancla temporal), total de movements_attributed (tope
    agregado) y el sentinela usado.
    """
    rows = (
        await session.execute(
            text(
                "SELECT decision_type, decision_data, created_at FROM decision_audit_log "
                "WHERE tenant_id = :tid AND decision_type IN (:merch, :brand) "
                "ORDER BY created_at ASC"
            ),
            {"tid": tid, "merch": _MERCH_COLLAPSE, "brand": _BRAND_COLLAPSE},
        )
    ).mappings().all()

    acc: dict[str, dict[str, Any]] = {}
    for r in rows:
        dd = _as_dict(r["decision_data"])
        sid = dd.get("supplier_id")
        if not sid:
            continue
        entry = acc.setdefault(
            sid,
            {
                "supplier_id": sid,
                "supplier_name": dd.get("supplier_name"),
                "has_merch": False,
                "sentinel_id": None,
                "product_ids": [],  # unión (para restaurar marca); se dedup al usar
                "merch_product_ids": set(),  # productos con compras colapsadas (MERCH)
                "collapse_at": None,  # created_at del último colapso MERCH (ancla)
                "movements_attributed": 0,  # total que el colapso re-apuntó (audit)
            },
        )
        if not entry["supplier_name"]:
            entry["supplier_name"] = dd.get("supplier_name")
        pids = [str(p) for p in (dd.get("product_ids") or [])]
        entry["product_ids"].extend(pids)
        if r["decision_type"] == _MERCH_COLLAPSE:
            entry["has_merch"] = True
            if not entry["sentinel_id"]:
                entry["sentinel_id"] = dd.get("collapsed_to_sentinel")
            entry["merch_product_ids"].update(pids)
            entry["collapse_at"] = r["created_at"]  # ORDER BY ASC → queda el último
            entry["movements_attributed"] += int(dd.get("movements_attributed") or 0)
    return list(acc.values())


async def _current_sentinel(session: AsyncSession, tid: uuid.UUID) -> uuid.UUID | None:
    """id del sentinela activo del tenant (o None). Read-only — NO lo crea."""
    row = (
        await session.execute(
            text(
                "SELECT id FROM suppliers "
                "WHERE tenant_id = :tid AND deactivated_at IS NULL "
                "AND custom_fields->>'_sentinel' = 'true' LIMIT 1"
            ),
            {"tid": tid},
        )
    ).first()
    return row[0] if row is not None else None


async def _count_sentinel_purchase_movements(
    session: AsyncSession,
    tid: uuid.UUID,
    product_ids: list[str],
    sentinel_id: str,
    collapse_at: Any,
) -> dict[str, int]:
    """Movimientos de compra PRE-COLAPSO que hoy cuelgan del sentinela, por producto.

    Una sola query por proveedor (no por producto — Neon es remota). Excluye
    anulados (voided_at: repair_inventory_ledger.py corre antes y voidea duplicados)
    y movimientos posteriores al colapso (created_at > collapse_at = compras
    genuinamente anónimas nuevas; el colapso solo pudo mover lo que ya existía).
    """
    if not product_ids:
        return {}
    stmt = text(
        "SELECT CAST(product_id AS text) AS pid, COUNT(*) AS n FROM inventory_movements "
        "WHERE tenant_id = :tid AND CAST(product_id AS text) IN :pids "
        "AND movement_type = 'purchase' AND supplier_id = CAST(:sentinel AS uuid) "
        "AND voided_at IS NULL AND created_at <= :collapse_at "
        "GROUP BY product_id"
    ).bindparams(bindparam("pids", expanding=True))
    rows = (
        await session.execute(
            stmt,
            {"tid": tid, "pids": product_ids, "sentinel": sentinel_id, "collapse_at": collapse_at},
        )
    ).all()
    return {str(r.pid): int(r.n) for r in rows}


async def _products_with_real_supplier_movements(
    session: AsyncSession,
    tid: uuid.UUID,
    product_ids: list[str],
    sentinel_id: str,
    self_supplier_id: str,
) -> set[str]:
    """Productos (de los dados) con movimientos de compra hacia un proveedor real.

    "Real" = supplier_id no nulo, distinto del sentinela y distinto del propio
    proveedor que reactivamos (para no marcar como ambiguo lo ya re-apuntado en una
    corrida previa). Señala que el producto tiene un origen verificado y por ende sus
    movimientos sentinela son ambiguos → no se tocan. Una sola query por proveedor.
    """
    if not product_ids:
        return set()
    stmt = text(
        "SELECT DISTINCT CAST(product_id AS text) AS pid FROM inventory_movements "
        "WHERE tenant_id = :tid AND CAST(product_id AS text) IN :pids "
        "AND movement_type = 'purchase' AND supplier_id IS NOT NULL "
        "AND supplier_id <> CAST(:sentinel AS uuid) "
        "AND supplier_id <> CAST(:sid AS uuid) "
        "AND voided_at IS NULL"
    ).bindparams(bindparam("pids", expanding=True))
    rows = (
        await session.execute(
            stmt,
            {"tid": tid, "pids": product_ids, "sentinel": sentinel_id, "sid": self_supplier_id},
        )
    ).all()
    return {str(r.pid) for r in rows}


async def _pending_marca_products(
    session: AsyncSession, tid: uuid.UUID, product_ids: list[str]
) -> list[str]:
    """Productos (de los dados) que aún tienen flags de reversa de marca. Read-only."""
    if not product_ids:
        return []
    stmt = text(
        "SELECT id FROM products WHERE tenant_id = :tid AND CAST(id AS text) IN :ids "
        "AND (custom_fields->>'_marca_prev' IS NOT NULL "
        "     OR custom_fields->>'_reassigned_from' IS NOT NULL)"
    ).bindparams(bindparam("ids", expanding=True))
    rows = (
        await session.execute(stmt, {"tid": tid, "ids": product_ids})
    ).all()
    return [str(r[0]) for r in rows]


async def _plan_tenant(session: AsyncSession, tid: uuid.UUID) -> list[dict[str, Any]]:
    """Plan de reversión del tenant (read-only). Una fila por marca-proveedor colapsada."""
    collapses = await _load_collapses(session, tid)
    if not collapses:
        return []

    fallback_sentinel = await _current_sentinel(session, tid)
    fallback = str(fallback_sentinel) if fallback_sentinel else None

    # Pase 1: resolver la existencia/estado de cada proveedor ANTES de armar claims —
    # un proveedor borrado (NOT_FOUND) o el sentinela no pueden "reclamar" productos
    # (forzarían multi-claim/LOW espurio en el claimant que sí va a revertir).
    supplier_rows: dict[str, Any] = {}
    for e in collapses:
        supplier_rows[e["supplier_id"]] = (
            await session.execute(
                text(
                    "SELECT deactivated_at, custom_fields, name FROM suppliers "
                    "WHERE id = :sid AND tenant_id = :tid"
                ),
                {"sid": e["supplier_id"], "tid": tid},
            )
        ).mappings().first()

    def _action_for(e: dict[str, Any]) -> str:
        sup = supplier_rows[e["supplier_id"]]
        if sup is None:
            return _ACT_NOT_FOUND
        scf = _as_dict(sup["custom_fields"])
        if is_sentinel_value(scf.get(SENTINEL_FLAG_KEY)) or sup["name"] == _SENTINEL_NAME:
            return _ACT_SKIP_SENTINEL
        return _ACT_REVERT

    # producto → marcas-proveedor MERCH VÁLIDAS que lo reclaman (multi-claim).
    claims: dict[str, set[str]] = {}
    for e in collapses:
        if _action_for(e) != _ACT_REVERT:
            continue
        for pid in e["merch_product_ids"]:
            claims.setdefault(pid, set()).add(e["supplier_id"])

    plans: list[dict[str, Any]] = []
    for e in collapses:
        sid = e["supplier_id"]
        sentinel = e["sentinel_id"] or fallback
        plan: dict[str, Any] = {
            "tenant_id": str(tid),
            "supplier_id": sid,
            "supplier_name": e["supplier_name"],
            "sentinel_id": sentinel,
            "collapse_at": e["collapse_at"],  # ancla temporal para el apply
            "action": _ACT_REVERT,
            "reactivate": False,
            "expense_ids": [],
            "movements_products_ok": [],
            "movements_products_low": [],
            "movements_stuck": 0,  # movimientos que quedan colgados del sentinela
            "marca_products": [],
        }

        action = _action_for(e)
        if action != _ACT_REVERT:
            plan["action"] = action
            plans.append(plan)
            continue
        sup = supplier_rows[sid]
        plan["reactivate"] = sup["deactivated_at"] is not None

        # Gastos aún colapsados a este proveedor (MERCH). Ancla exacta e idempotente.
        if e["has_merch"] and sentinel:
            exps = (
                await session.execute(
                    text(
                        "SELECT id FROM expense_entries "
                        "WHERE tenant_id = :tid AND supplier_id = CAST(:sentinel AS uuid) "
                        "AND custom_fields->>'_supplier_prev' = :sid AND voided_at IS NULL"
                    ),
                    {"tid": tid, "sentinel": sentinel, "sid": sid},
                )
            ).all()
            plan["expense_ids"] = [str(r[0]) for r in exps]

        # Movimientos de compra: candidatos = pre-colapso y hoy en el sentinela
        # (batcheado: 2 queries por proveedor, no 2 por producto).
        if e["has_merch"] and sentinel and e["collapse_at"] is not None:
            merch_pids = sorted(e["merch_product_ids"])
            counts = await _count_sentinel_purchase_movements(
                session, tid, merch_pids, sentinel, e["collapse_at"]
            )
            real_pids = await _products_with_real_supplier_movements(
                session, tid, [p for p in merch_pids if counts.get(p)], sentinel, sid
            )
            ok_total = 0
            for pid in merch_pids:
                m = counts.get(pid, 0)
                if m == 0:
                    continue  # ya re-apuntado o nunca colgó
                multi = len(claims.get(pid, set())) > 1
                if not multi and pid not in real_pids:
                    plan["movements_products_ok"].append(pid)
                    ok_total += m
                else:
                    plan["movements_products_low"].append(pid)
                    plan["movements_stuck"] += m

            # Tope agregado (unidades correctas: movimientos vs movimientos): si los
            # candidatos superan lo que el colapso registró haber movido, había
            # compras sentinela previas al colapso mezcladas → no se adivina cuáles.
            if ok_total > e["movements_attributed"]:
                plan["movements_products_low"].extend(plan["movements_products_ok"])
                plan["movements_stuck"] += ok_total
                plan["movements_products_ok"] = []

        # Marca a restaurar: productos del audit que aún tienen los flags de reversa.
        dedup_pids = list(dict.fromkeys(e["product_ids"]))
        plan["marca_products"] = await _pending_marca_products(session, tid, dedup_pids)

        has_writes = (
            plan["reactivate"]
            or bool(plan["expense_ids"])
            or bool(plan["movements_products_ok"])
            or bool(plan["marca_products"])
        )
        if not has_writes:
            plan["action"] = _ACT_ALREADY_ACTIVE
        plans.append(plan)

    return plans


async def _apply_revert(
    session: AsyncSession, tid: uuid.UUID, plans: list[dict[str, Any]]
) -> int:
    """Aplica el plan de reversión. Reversible y auditado. Solo procesa acción REVERT."""
    applied = 0
    for plan in plans:
        if plan["action"] != _ACT_REVERT:
            continue
        sid = plan["supplier_id"]
        sentinel = plan["sentinel_id"]

        sup = (
            await session.execute(
                text(
                    "SELECT custom_fields, name FROM suppliers "
                    "WHERE id = :sid AND tenant_id = :tid"
                ),
                {"sid": sid, "tid": tid},
            )
        ).mappings().first()
        if sup is None:
            continue
        supplier_name = sup["name"]

        # 1. Reactivar + marcar provisional SOLO si está dado de baja. No pisar un
        #    proveedor ya activo (puede haber sido validado a mano).
        if plan["reactivate"]:
            scf = _as_dict(sup["custom_fields"])
            scf[_PROVISIONAL_FLAG] = True
            await session.execute(
                text(
                    "UPDATE suppliers SET deactivated_at = NULL, "
                    "custom_fields = CAST(:cf AS jsonb) "
                    "WHERE id = :sid AND tenant_id = :tid"
                ),
                {"cf": json.dumps(scf), "sid": sid, "tid": tid},
            )

        # 2. Devolver los gastos a su proveedor original (ancla _supplier_prev).
        if plan["expense_ids"]:
            await session.execute(
                text(
                    "UPDATE expense_entries "
                    "SET supplier_id = CAST(:sid AS uuid), supplier_name = :sname, "
                    "    custom_fields = custom_fields - '_supplier_prev' "
                    "WHERE tenant_id = :tid AND supplier_id = CAST(:sentinel AS uuid) "
                    "AND custom_fields->>'_supplier_prev' = :sid AND voided_at IS NULL"
                ),
                {"sid": sid, "sname": supplier_name, "tid": tid, "sentinel": sentinel},
            )

        # 3. Re-apuntar movimientos de compra de los productos ALTA PRECISIÓN.
        #    Un solo UPDATE por proveedor, con la MISMA ancla temporal del plan
        #    (created_at <= collapse_at): una compra anónima registrada por la app
        #    entre plan y apply queda afuera por construcción (no hay TOCTOU).
        movements_repointed = 0
        if plan["movements_products_ok"] and plan["collapse_at"] is not None:
            stmt = text(
                "UPDATE inventory_movements SET supplier_id = CAST(:sid AS uuid) "
                "WHERE tenant_id = :tid AND CAST(product_id AS text) IN :pids "
                "AND movement_type = 'purchase' "
                "AND supplier_id = CAST(:sentinel AS uuid) "
                "AND voided_at IS NULL AND created_at <= :collapse_at"
            ).bindparams(bindparam("pids", expanding=True))
            mres = await session.execute(
                stmt,
                {
                    "sid": sid,
                    "tid": tid,
                    "pids": plan["movements_products_ok"],
                    "sentinel": sentinel,
                    "collapse_at": plan["collapse_at"],
                },
            )
            movements_repointed = getattr(mres, "rowcount", 0) or 0

        # 4. Restaurar la marca del producto desde _marca_prev; limpiar flags.
        marca_restored = await _restore_product_marca(session, tid, plan["marca_products"])

        # 5. Auditoría.
        decision_data = {
            "supplier_id": str(sid),
            "supplier_name": supplier_name,
            "reactivated": plan["reactivate"],
            "expense_ids": plan["expense_ids"],
            "sentinel_id": sentinel,
            "movement_products_ok": plan["movements_products_ok"],
            "movements_repointed": movements_repointed,
            "movement_products_low_confidence": plan["movements_products_low"],
            "movements_left_in_sentinel": plan["movements_stuck"],
            "marca_restored_products": marca_restored,
        }
        await session.execute(
            text(
                "INSERT INTO decision_audit_log "
                "(id, tenant_id, decision_type, decision_data, triggered_by, created_at) "
                "VALUES (gen_random_uuid(), :tid, :dt, CAST(:dd AS jsonb), :tb, now())"
            ),
            {
                "tid": tid,
                "dt": _REVERT_DECISION,
                "dd": json.dumps(decision_data),
                "tb": "script:revert_brand_supplier_collapse",
            },
        )
        applied += 1
    return applied


async def _restore_product_marca(
    session: AsyncSession, tid: uuid.UUID, product_ids: list[str]
) -> list[str]:
    """Restaura marca desde _marca_prev en los productos que lo tengan. Limpia flags.

    Batcheado (2 UPDATEs por proveedor, no SELECT+UPDATE por producto): con Neon
    remota el per-fila alargaba la transacción (y sus locks) decenas de segundos.
    """
    if not product_ids:
        return []
    # Con _marca_prev: restaurar marca + limpiar ambos flags, en un solo UPDATE jsonb.
    restore_stmt = text(
        "UPDATE products SET custom_fields = "
        "(custom_fields - '_marca_prev' - '_reassigned_from') "
        "|| jsonb_build_object('marca', custom_fields->>'_marca_prev') "
        "WHERE tenant_id = :tid AND CAST(id AS text) IN :pids "
        "AND custom_fields->>'_marca_prev' IS NOT NULL "
        "RETURNING CAST(id AS text)"
    ).bindparams(bindparam("pids", expanding=True))
    restored_rows = (
        await session.execute(restore_stmt, {"tid": tid, "pids": product_ids})
    ).all()
    # Solo _reassigned_from (sin marca previa que devolver): limpiar el flag.
    cleanup_stmt = text(
        "UPDATE products SET custom_fields = custom_fields - '_reassigned_from' "
        "WHERE tenant_id = :tid AND CAST(id AS text) IN :pids "
        "AND custom_fields->>'_marca_prev' IS NULL "
        "AND custom_fields->>'_reassigned_from' IS NOT NULL"
    ).bindparams(bindparam("pids", expanding=True))
    await session.execute(cleanup_stmt, {"tid": tid, "pids": product_ids})
    return [str(r[0]) for r in restored_rows]


def _print_summary(tid: uuid.UUID, plans: list[dict[str, Any]]) -> None:
    by_action = Counter(p["action"] for p in plans)
    print(f"tenant {tid}: {len(plans)} colapso(s) — {dict(by_action)}")
    for p in plans:
        low = p["movements_products_low"]
        # Solo REVERT o filas con LOW_CONFIDENCE tienen algo que decir acá.
        if p["action"] != _ACT_REVERT and not low:
            continue
        note = (
            f"  ⚠ {len(low)} producto(s)/{p['movements_stuck']} mov. LOW_CONFIDENCE "
            f"(quedan en '{_SENTINEL_NAME}')"
            if low
            else ""
        )
        if p["action"] == _ACT_REVERT:
            print(
                f"    [REVERT] {p['supplier_name']!r} supplier={p['supplier_id']} — "
                f"{len(p['expense_ids'])} gasto(s), {len(p['movements_products_ok'])} "
                f"producto(s) OK{note}"
            )
        elif low:
            print(
                f"    [{p['action']}] {p['supplier_name']!r} supplier={p['supplier_id']}"
                f"{note}"
            )


def _write_report(path: str, plans: list[dict[str, Any]]) -> None:
    rows = [
        {
            "tenant_id": p["tenant_id"],
            "supplier_id": p["supplier_id"],
            "supplier_name": p["supplier_name"],
            "action": p["action"],
            "expenses_to_revert": len(p["expense_ids"]),
            "movements_ok_products": len(p["movements_products_ok"]),
            "movements_low_products": len(p["movements_products_low"]),
            "movements_left_in_sentinel": p["movements_stuck"],
            "low_confidence_product_ids": ";".join(p["movements_products_low"]),
        }
        for p in plans
    ]
    if path.lower().endswith(".csv"):
        with open(path, "w", newline="", encoding="utf-8-sig") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()) if rows else [])
            writer.writeheader()
            writer.writerows(rows)
    else:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(rows, fh, ensure_ascii=False, indent=2)
    print(f"\nReporte escrito en {path} ({len(rows)} fila(s)).")


async def main() -> None:
    parser = argparse.ArgumentParser(description="Revierte el colapso de marcas→proveedor.")
    parser.add_argument("--tenant", help="UUID de tenant puntual")
    parser.add_argument("--all-active", action="store_true", help="Todos los tenants activos")
    parser.add_argument("--apply", action="store_true", help="Escribir cambios (default: dry-run)")
    parser.add_argument("--out", help="Path del reporte (CSV si .csv, si no JSON)")
    args = parser.parse_args()

    if not args.tenant and not args.all_active:
        print("ERROR: indicá --tenant <uuid> o --all-active.")
        sys.exit(2)

    url, connect_args = async_engine_config()
    engine = create_async_engine(url, connect_args=connect_args)
    async with AsyncSession(engine) as session:
        if args.tenant:
            tids = [uuid.UUID(args.tenant)]
        else:
            rows = await session.execute(
                text("SELECT tenant_id FROM tenants WHERE status = 'ACTIVE'")
            )
            tids = [r[0] for r in rows.all()]

        mode = "APPLY" if args.apply else "DRY-RUN"
        print(f"[{mode}] reversión de marcas→proveedor en {len(tids)} tenant(s)\n")

        all_plans: list[dict[str, Any]] = []
        total_applied = 0
        for tid in tids:
            plans = await _plan_tenant(session, tid)
            if not plans:
                continue
            all_plans.extend(plans)
            _print_summary(tid, plans)
            if args.apply:
                total_applied += await _apply_revert(session, tid, plans)
            print()

        if args.out and all_plans:
            _write_report(args.out, all_plans)

        stuck = sum(p["movements_stuck"] for p in all_plans)
        if args.apply:
            await session.commit()
            print(
                f"COMMIT: {total_applied} marca-proveedor reactivada(s) "
                f"(decision_type={_REVERT_DECISION}). {stuck} movimiento(s) quedaron "
                f"LOW_CONFIDENCE en '{_SENTINEL_NAME}' — revisar el reporte y reasignar a mano."
            )
        else:
            await session.rollback()
            revert = sum(1 for p in all_plans if p["action"] == _ACT_REVERT)
            print(
                f"Dry-run: nada se escribió. Con --apply se revertirían {revert} colapso(s); "
                f"{stuck} movimiento(s) quedarían LOW_CONFIDENCE sin tocar."
            )
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
