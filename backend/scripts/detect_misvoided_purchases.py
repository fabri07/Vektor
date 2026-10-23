"""Read-only global: detecta compras (``movement_type='purchase'``) VOIDEADAS por las
reparaciones de inventario que probablemente NO eran duplicados reales.

CONTEXTO: el dedup por timing de ``repair_inventory_ledger.py`` (B1) agrupa
movimientos VIVOS por ``(product_id, movement_type, date(COALESCE(occurred_at,
created_at)), qty, unit_cost)`` y voidea los "extras" del cluster. Caso real "don
pedro" (2026-07): ``inventory_movements.created_at`` quedó con la fecha de CARGA del
archivo (no la fecha real de la compra) para varios productos, y dos compras reales
de meses distintos con la misma cantidad (packs de proveedor de tamaño fijo) cayeron
en el mismo cluster y se voidearon de más. Esto pudo haber pasado en otros tenants.
Decisión del usuario: **solo detectar y reportar** — la reparación es human-in-the-loop,
caso por caso (ver epílogo de este script y ``fix_reconciled_stock.py`` como
precedente de reparación puntual ya aplicada).

ALGORITMO (por tenant)
----------------------
1. Compras vivas voideadas: ``SELECT ... FROM inventory_movements WHERE
   movement_type='purchase' AND voided_at IS NOT NULL``.
2. De esas, cuáles fueron voideadas POR una reparación auditada
   (``decision_type IN ('INVENTORY_REPAIR', 'INVENTORY_RECONCILIATION_FIX')``):
   primero se intenta extracción ESTRUCTURADA — ``repair_inventory_ledger.py`` audita
   el paso B1 (dedup) con ``decision_data['voided_movement_ids']`` (lista de ids). Si
   una corrida no tuviera esa clave exacta, se cae al FALLBACK documentado: buscar el
   id del movimiento como substring, pero SOLO dentro de los campos que representan
   evidencia de void (``_VOID_EVIDENCE_KEYS`` — ``voided_movement_ids`` de B1 y
   ``disputed_movement_ids`` de ``fix_reconciled_stock.py``), NUNCA contra el blob
   completo de ``decision_data``. Motivo: ``repair_inventory_ledger.py`` audita sus 3
   sub-pasos (B1/B2/B3) bajo el MISMO ``decision_type='INVENTORY_REPAIR'``, y el paso
   B2 (backfill de COGS) audita ``source_movement_ids`` — movimientos VIVOS (no
   voideados) que generaron el gasto. Buscar el substring contra todo el JSON podía
   matchear una compra viva citada en un B2 y atribuirle un ``repair_audit_id`` de una
   fila que nunca la voideó (falsa atribución + contamina el insert de ``--audit``).
3. Para cada compra voideada matcheada, se buscan los socios de cluster
   SOBREVIVIENTES: movimientos VIVOS del mismo
   ``(product_id, movement_type='purchase', date(COALESCE(occurred_at, created_at)),
   qty, unit_cost)`` — la misma clave que usó el dedup.
4. Señales de NO-duplicado (``_score_candidate``, pura y testeada aparte):
     - ``distinct_uploads``: la voideada y algún socio tienen ``source_upload_id``
       ambos no nulos y DISTINTOS (dos archivos ⇒ dos compras reales).
     - ``distinct_hashes``: ídem con ``source_row_hash``.
     - ``integrity_divergence_negative``: el producto aparece con ``diff < 0`` (el
       sistema muestra MENOS de lo esperado) en el ÚLTIMO audit
       ``INVENTORY_INTEGRITY_DIVERGENCE`` del tenant — evidencia de que faltan compras
       cargadas en el ledger.
   ``misvoid_score`` = cuántas señales aplican (0–3).
5. Se reportan TODAS las compras voideadas por reparación (aunque score=0), ordenadas
   por score descendente.

READ-ONLY: cero UPDATE/INSERT sobre datos de negocio. La ÚNICA escritura permitida es
el INSERT opcional en ``decision_audit_log`` con ``--audit`` (un insert por tenant con
los hallazgos score>=1) — se hace ``commit()`` solo de eso; todo lo demás queda en
``rollback()``. NUNCA imprime la connection URL (la provee el usuario desde su shell).

REPARACIÓN (manual, caso por caso — este script NO repara nada): para reactivar una
compra mal-voideada, usar la mecánica INCREMENTAL de
``app/application/services/stock_service.py::unvoid_movement`` —
``UPDATE inventory_movements SET voided_at = NULL ...`` + ``stock_units += qty`` +
sincronizar ``inventory_balances`` — NUNCA recomputar ``stock_units`` desde el ledger
(``stock_units`` no es Σ(inventory_movements): tiene base no-ledger de alta manual/
catálogo). Ver ``fix_reconciled_stock.py`` como precedente de una reparación puntual
ya verificada a mano contra archivos fuente.

Usage:
    # Un tenant puntual
    DATABASE_URL='postgresql://...neon.tech/vektor?sslmode=require' \
        .venv/bin/python scripts/detect_misvoided_purchases.py \
        --tenant ee2625dc-96b7-464c-bda3-7f7018cc2a5b --out misvoided_report.csv

    # Todos los tenants activos
    ... scripts/detect_misvoided_purchases.py --all-active --out misvoided_report.csv

    # Con auditoría (además del reporte)
    ... scripts/detect_misvoided_purchases.py --all-active --audit
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _db import (  # noqa: E402
    REPAIR_DECISION_TYPES,
    async_engine_config,
    insert_decision_audit,
)
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine  # noqa: E402

_DECISION_TYPE = "INVENTORY_MISVOIDED_PURCHASE_FINDING"
_TRIGGERED_BY = "script:detect_misvoided_purchases"

# decision_types de reparaciones auditadas que pudieron voidear compras (mismo
# universo que diag_missing_purchases_scope.py / reconcile_untagged_adjustments.py).
_REPAIR_DECISION_TYPES = REPAIR_DECISION_TYPES

_DIVERGENCE_DECISION_TYPE = "INVENTORY_INTEGRITY_DIVERGENCE"


def _as_dict(value: Any) -> dict[str, Any] | None:
    """decision_data puede venir como dict (asyncpg JSONB) o como str JSON."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (ValueError, TypeError):
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


# Keys de decision_data que constituyen EVIDENCIA DE VOID (ids de movimientos a los
# que efectivamente se les setea voided_at). Verificado leyendo los INSERT a
# decision_audit_log de cada script:
#   - repair_inventory_ledger.py, paso B1 (dedup, _apply_b1_dedup):
#       decision_data['voided_movement_ids'] = void_ids  -> SÍ voideados.
#   - repair_inventory_ledger.py, paso B2 (backfill COGS, _apply_b2_backfill):
#       decision_data['source_movement_ids'] = ids de movimientos VIVOS (no
#       voideados; solo generaron un gasto COGS de backfill) — EXCLUIDA a propósito.
#       Mismo decision_type='INVENTORY_REPAIR' que B1, así que si no se filtrara por
#       key una compra viva citada en un B2 podría matchear por accidente contra esa
#       fila y quedar mal atribuida como "voideada por reparación".
#       decision_data['created_expense_ids'] = ids de ExpenseEntry, no de
#       inventory_movements — tampoco es evidencia de void de una compra.
#   - repair_inventory_ledger.py, paso B3 (ajuste de stock, _apply_b3_adjust):
#       decision_data no tiene ids de movimiento (solo agregados por producto).
#   - fix_reconciled_stock.py (_apply_product): decision_data = plan completo, cuya
#       única key con movimientos a los que se les setea voided_at=now() es
#       'disputed_movement_ids' (ver _apply_product: UPDATE ... SET voided_at = now()
#       ... WHERE id IN disputed_movement_ids). 'disputed_sum' es un agregado, no ids.
_VOID_EVIDENCE_KEYS = ("voided_movement_ids", "disputed_movement_ids")


def _void_evidence_blob(dd: dict[str, Any]) -> str | None:
    """Serializa SOLO las keys de ``dd`` que son evidencia de void (ver
    ``_VOID_EVIDENCE_KEYS``). Devuelve ``None`` si ninguna está presente (nada
    matcheable en esta fila sin caer en el blob completo).
    """
    parts = [dd[key] for key in _VOID_EVIDENCE_KEYS if key in dd]
    if not parts:
        return None
    return json.dumps(parts, default=str)


def _build_repair_audit_map(
    voided_purchase_ids: set[str], audit_rows: list[Any]
) -> dict[str, str]:
    """Mapea movement_id (compra voideada) -> id del decision_audit_log que la voideó.

    Pase 1 (estructurado): ``decision_data['voided_movement_ids']`` — la clave que
    ``repair_inventory_ledger.py`` audita en el paso B1 del dedup. Pase 2 (fallback,
    solo para los ids que el pase 1 no resolvió): el id como substring, pero
    ACOTADO a las keys de ``_VOID_EVIDENCE_KEYS`` (nunca el JSON completo de
    ``decision_data``) — así una corrida con formato levemente distinto de
    ``voided_movement_ids``/``disputed_movement_ids`` igual matchea, pero un id VIVO
    citado en ``source_movement_ids`` (B2) o en cualquier otra key no-void jamás
    produce un match espurio.
    """
    mapping: dict[str, str] = {}

    for row in audit_rows:
        dd = _as_dict(row["decision_data"])
        if dd is None:
            continue
        ids = dd.get("voided_movement_ids")
        if not isinstance(ids, list):
            continue
        for mid in ids:
            key = str(mid)
            if key in voided_purchase_ids and key not in mapping:
                mapping[key] = str(row["id"])

    remaining = voided_purchase_ids - mapping.keys()
    if remaining:
        for row in audit_rows:
            if not remaining:
                break
            dd = _as_dict(row["decision_data"])
            if dd is None:
                continue
            blob = _void_evidence_blob(dd)
            if blob is None:
                continue
            for mid in list(remaining):
                if mid in blob:
                    mapping[mid] = str(row["id"])
                    remaining.discard(mid)

    return mapping


def _negative_divergence_product_ids(decision_data: Any) -> set[str]:
    """product_ids con ``diff < 0`` en un audit ``INVENTORY_INTEGRITY_DIVERGENCE``.

    ``diff < 0`` = el sistema muestra MENOS stock que el esperado — evidencia de que
    faltan compras vivas en el ledger (consistente con "se voideó una compra real").
    None-safe: ``decision_data`` ausente/mal formado o ``diff`` ausente -> se ignora.
    """
    dd = _as_dict(decision_data)
    if dd is None:
        return set()
    divergences = dd.get("divergences")
    if not isinstance(divergences, list):
        return set()
    out: set[str] = set()
    for d in divergences:
        if not isinstance(d, dict):
            continue
        diff = d.get("diff")
        pid = d.get("product_id")
        if diff is not None and pid is not None and diff < 0:
            out.add(str(pid))
    return out


def _score_candidate(
    voided: dict[str, Any],
    partners: list[dict[str, Any]],
    divergent_product_ids: set[str],
) -> dict[str, Any]:
    """Señales de NO-duplicado para una compra voideada, contra sus socios de cluster.

    Pura y testeable. None-safe: ``source_upload_id``/``source_row_hash`` ausentes
    (None) nunca cuentan como "distintos" — solo se marca la señal cuando AMBOS lados
    (voideada y algún socio) tienen el dato Y son distintos. Sin socios, las señales
    partner-dependientes quedan en False (no hay evidencia, no un True espurio).
    """
    partner_ids = [str(p["id"]) for p in partners]

    def _distinct(field: str) -> bool:
        v_val = voided.get(field)
        if v_val is None:
            return False
        for p in partners:
            p_val = p.get(field)
            if p_val is not None and str(p_val) != str(v_val):
                return True
        return False

    distinct_uploads = _distinct("source_upload_id")
    distinct_hashes = _distinct("source_row_hash")
    integrity_divergence_negative = str(voided.get("product_id")) in divergent_product_ids

    misvoid_score = (
        int(distinct_uploads) + int(distinct_hashes) + int(integrity_divergence_negative)
    )
    return {
        "partner_movement_ids": partner_ids,
        "distinct_uploads": distinct_uploads,
        "distinct_hashes": distinct_hashes,
        "integrity_divergence_negative": integrity_divergence_negative,
        "misvoid_score": misvoid_score,
    }


async def _find_partners(
    session: AsyncSession, tid: str, voided: dict[str, Any]
) -> list[dict[str, Any]]:
    """Movimientos VIVOS del mismo cluster de dedup que la compra voideada."""
    occurred_at = voided.get("occurred_at")
    occurred = occurred_at if occurred_at is not None else voided["created_at"]
    day = occurred.date()
    rows = (
        await session.execute(
            text(
                "SELECT id, source_upload_id, source_row_hash FROM inventory_movements "
                "WHERE tenant_id = CAST(:tid AS uuid) AND product_id = CAST(:pid AS uuid) "
                "AND movement_type = 'purchase' AND voided_at IS NULL "
                "AND date(COALESCE(occurred_at, created_at)) = :d AND qty = :qty "
                "AND unit_cost IS NOT DISTINCT FROM :uc"
            ),
            {
                "tid": tid,
                "pid": str(voided["product_id"]),
                "d": day,
                "qty": voided["qty"],
                "uc": voided["unit_cost"],
            },
        )
    ).mappings().all()
    return [dict(r) for r in rows]


async def _plan_tenant(session: AsyncSession, tid: str) -> list[dict[str, Any]]:
    """Plan (read-only) de compras mal-voideadas del tenant. Una fila por compra."""
    voided_purchases = (
        await session.execute(
            text(
                "SELECT im.id, im.product_id, p.name AS product_name, im.qty, "
                "im.unit_cost, im.created_at, im.occurred_at, im.voided_at, "
                "im.source_upload_id, im.source_row_hash "
                "FROM inventory_movements im "
                "JOIN products p ON p.id = im.product_id AND p.tenant_id = im.tenant_id "
                "WHERE im.tenant_id = CAST(:tid AS uuid) AND im.movement_type = 'purchase' "
                "AND im.voided_at IS NOT NULL"
            ),
            {"tid": tid},
        )
    ).mappings().all()
    if not voided_purchases:
        return []

    voided_ids = {str(v["id"]) for v in voided_purchases}

    audit_rows = (
        await session.execute(
            text(
                "SELECT id, decision_data FROM decision_audit_log "
                "WHERE tenant_id = CAST(:tid AS uuid) AND decision_type = ANY(:types)"
            ),
            {"tid": tid, "types": list(_REPAIR_DECISION_TYPES)},
        )
    ).mappings().all()
    repair_audit_by_mv = _build_repair_audit_map(voided_ids, audit_rows)

    matched = [dict(v) for v in voided_purchases if str(v["id"]) in repair_audit_by_mv]
    if not matched:
        return []

    div_row = (
        await session.execute(
            text(
                "SELECT decision_data FROM decision_audit_log "
                "WHERE tenant_id = CAST(:tid AS uuid) AND decision_type = :dt "
                "ORDER BY created_at DESC LIMIT 1"
            ),
            {"tid": tid, "dt": _DIVERGENCE_DECISION_TYPE},
        )
    ).mappings().first()
    divergent_product_ids = _negative_divergence_product_ids(
        div_row["decision_data"] if div_row is not None else None
    )

    items: list[dict[str, Any]] = []
    for v in matched:
        partners = await _find_partners(session, tid, v)
        score = _score_candidate(v, partners, divergent_product_ids)
        items.append(
            {
                "tenant_id": tid,
                "product_id": str(v["product_id"]),
                "product_name": v.get("product_name"),
                "movement_id": str(v["id"]),
                "qty": v["qty"],
                "unit_cost": (None if v["unit_cost"] is None else float(v["unit_cost"])),
                "voided_at": v["voided_at"].isoformat() if v["voided_at"] is not None else None,
                "repair_audit_id": repair_audit_by_mv[str(v["id"])],
                **score,
            }
        )

    items.sort(key=lambda it: it["misvoid_score"], reverse=True)
    return items


def _print_summary(tid: str, items: list[dict[str, Any]]) -> None:
    n_ge1 = sum(1 for it in items if it["misvoid_score"] >= 1)
    n_ge2 = sum(1 for it in items if it["misvoid_score"] >= 2)
    print(
        f"tenant {tid}: {len(items)} compra(s) voideada(s) por reparación — "
        f"score>=1: {n_ge1}   score>=2: {n_ge2}"
    )


def _write_report(path: str, items: list[dict[str, Any]]) -> None:
    fieldnames = [
        "tenant_id",
        "product_id",
        "product_name",
        "movement_id",
        "qty",
        "unit_cost",
        "voided_at",
        "partner_movement_ids",
        "distinct_uploads",
        "distinct_hashes",
        "integrity_divergence_negative",
        "misvoid_score",
        "repair_audit_id",
    ]
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for it in items:
            writer.writerow(
                {
                    "tenant_id": it["tenant_id"],
                    "product_id": it["product_id"],
                    "product_name": it["product_name"],
                    "movement_id": it["movement_id"],
                    "qty": it["qty"],
                    "unit_cost": it["unit_cost"],
                    "voided_at": it["voided_at"],
                    "partner_movement_ids": ";".join(it["partner_movement_ids"]),
                    "distinct_uploads": it["distinct_uploads"],
                    "distinct_hashes": it["distinct_hashes"],
                    "integrity_divergence_negative": it["integrity_divergence_negative"],
                    "misvoid_score": it["misvoid_score"],
                    "repair_audit_id": it["repair_audit_id"],
                }
            )
    print(f"\nReporte escrito en {path} ({len(items)} fila(s)).")


async def _audit_tenant(session: AsyncSession, tid: str, items: list[dict[str, Any]]) -> bool:
    """Inserta UN decision_audit_log con los hallazgos score>=1 del tenant.

    Devuelve True si insertó (hay hallazgos), False si no había nada que auditar.
    """
    findings = [it for it in items if it["misvoid_score"] >= 1]
    if not findings:
        return False
    totals = {
        "total_voided_by_repair": len(items),
        "score_ge_1": len(findings),
        "score_ge_2": sum(1 for it in items if it["misvoid_score"] >= 2),
    }
    decision_data = {"findings": findings, "totals": totals}
    await insert_decision_audit(
        session,
        tenant_id=tid,
        decision_type=_DECISION_TYPE,
        decision_data=decision_data,
        triggered_by=_TRIGGERED_BY,
    )
    return True


async def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Detecta compras voideadas por reparaciones que probablemente NO eran duplicados."
        )
    )
    parser.add_argument("--tenant", help="UUID de tenant puntual")
    parser.add_argument("--all-active", action="store_true", help="Todos los tenants activos")
    parser.add_argument(
        "--out",
        default="misvoided_report.csv",
        help="Path del reporte CSV (default misvoided_report.csv)",
    )
    parser.add_argument(
        "--audit",
        action="store_true",
        help="Insertar hallazgos (score>=1) en decision_audit_log (única escritura permitida)",
    )
    args = parser.parse_args()

    if not args.tenant and not args.all_active:
        print("ERROR: indicá --tenant <uuid> o --all-active.")
        sys.exit(2)

    url, connect_args = async_engine_config()
    engine = create_async_engine(url, connect_args=connect_args)
    async with AsyncSession(engine) as session:
        if args.tenant:
            tids = [args.tenant]
        else:
            rows = await session.execute(
                # Canónico uppercase: la app solo escribe 'ACTIVE'/'TRIAL' (ver deps.py).
                text("SELECT tenant_id FROM tenants WHERE status IN ('ACTIVE', 'TRIAL')")
            )
            tids = [str(r[0]) for r in rows.all()]

        print(
            f"[READ-ONLY] detección de compras mal-voideadas en {len(tids)} tenant(s)\n"
        )

        all_items: list[dict[str, Any]] = []
        audited_tenants = 0
        for tid in tids:
            items = await _plan_tenant(session, tid)
            if not items:
                continue
            all_items.extend(items)
            _print_summary(tid, items)
            if args.audit and await _audit_tenant(session, tid, items):
                audited_tenants += 1

        if args.out:
            _write_report(args.out, all_items)

        if args.audit:
            await session.commit()
            print(
                f"\nCOMMIT: {audited_tenants} tenant(s) auditado(s) "
                f"(decision_type={_DECISION_TYPE}). El resto de la corrida fue solo lectura."
            )
        else:
            await session.rollback()
            print(
                "\nDry read-only: nada se escribió. Usá --audit para dejar "
                "constancia en decision_audit_log."
            )

        print(
            "\nReparación: es MANUAL, caso por caso — usar la mecánica INCREMENTAL de "
            "stock_service.unvoid_movement (UPDATE inventory_movements SET voided_at=NULL "
            "+ stock_units += qty + sincronizar inventory_balances). NUNCA recomputar "
            "stock_units desde el ledger (no es Σ(inventory_movements); tiene base no-ledger)."
        )
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
