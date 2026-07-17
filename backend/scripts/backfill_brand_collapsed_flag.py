"""Backfill del flag ``_brand_collapsed`` en marcas ya colapsadas por el cleanup.

Contexto: ``deactivate_brand_suppliers.py`` dio de baja las marcas confundidas con
proveedores seteando SOLO ``deactivated_at`` (la corrida original no dejaba marcador
en la fila). El listado y el reactivate ahora distinguen "marca colapsada por error"
de "proveedor real dado de baja" por ``custom_fields['_brand_collapsed']``, así que
las bajas históricas del cleanup necesitan el flag. La fuente de verdad es la traza
de la corrida original en ``decision_audit_log``:

    decision_type IN ('SUPPLIER_BRAND_CLEANUP', 'SUPPLIER_MERCH_SOURCE_COLLAPSE',
                      'SUPPLIER_PROVISIONAL_COLLAPSE')
    AND triggered_by IN ('script:deactivate_brand_suppliers',
                         'script:collapse_provisional_brand_suppliers')
    → decision_data->>'supplier_id'

RESULTADOS (contados y reportados en --out):
    TAG                  → desactivado por el cleanup (deactivated_at coincide con la
                           traza), sin flag → se taggea (solo con --apply).
    SKIP_ACTIVE          → hoy está activo (fue restaurado a propósito) → no tocar.
    SKIP_ALREADY_FLAGGED → ya tiene el flag (idempotencia) → no tocar.
    SKIP_REDEACTIVATED   → deactivated_at NO coincide con la traza del cleanup: fue
                           restaurado y vuelto a dar de baja por un humano (baja de
                           negocio real) → NO taggear; revisar a mano si hace falta.
    NOT_FOUND            → el supplier de la traza ya no existe → informativo.

NOTA: tener '_provisional_from_brand' NO exime del tag — la población real de Neon
son marcas que NACIERON provisionales (las creó el script de revert) y DESPUÉS las
colapsó el cleanup: quedaron con ambos flags. Lo que decide es el match temporal
de deactivated_at contra la traza (un provisional restaurado y activo cae en
SKIP_ACTIVE; uno re-dado-de-baja por un humano, en SKIP_REDEACTIVATED).

Usage:
    # Dry-run (default) de un tenant puntual:
    DATABASE_URL='postgresql://...' .venv/bin/python \
        scripts/backfill_brand_collapsed_flag.py --tenant <uuid>

    # Dry-run global:
    ... scripts/backfill_brand_collapsed_flag.py --all-active

    # Reporte detallado (CSV por extensión .csv, si no JSON):
    ... scripts/backfill_brand_collapsed_flag.py --all-active --out reporte.csv

    # Aplicar (después de revisar el dry-run):
    ... scripts/backfill_brand_collapsed_flag.py --all-active --apply

REVERSIBLE: cada tag queda auditado (decision_type='SUPPLIER_BRAND_COLLAPSED_FLAG_BACKFILL',
con supplier_id + before/after del flag). Undo en bloque:

    UPDATE suppliers SET custom_fields = custom_fields - '_brand_collapsed'
      WHERE id IN (
        SELECT (decision_data->>'supplier_id')::uuid
        FROM decision_audit_log
        WHERE tenant_id = '<tid>'
          AND decision_type = 'SUPPLIER_BRAND_COLLAPSED_FLAG_BACKFILL'
      );

NUNCA imprime la connection URL. Read-only por default. Correr desde backend/.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import sys
import uuid
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _db import SQL_SET_BRAND_COLLAPSED, async_engine_config, insert_decision_audit  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine  # noqa: E402

from app.persistence.models._sentinel import is_flag_true  # noqa: E402
from app.persistence.models.supplier import BRAND_COLLAPSED_FLAG_KEY  # noqa: E402

_DECISION_TYPE = "SUPPLIER_BRAND_COLLAPSED_FLAG_BACKFILL"
# Fuentes de traza de colapso de marcas. Incluye el colapso one-off de
# marcas-provisionales de don pedro (2026-07-09, 49 filas,
# 'script:collapse_provisional_brand_suppliers' — el script no vive en el repo,
# pero su auditoría sí): esa corrida es la baja VIGENTE de esas 49, la traza del
# cleanup de junio quedó obsoleta cuando el revert las restauró en el medio.
_SOURCE_DECISION_TYPES = (
    "SUPPLIER_BRAND_CLEANUP",
    "SUPPLIER_MERCH_SOURCE_COLLAPSE",
    "SUPPLIER_PROVISIONAL_COLLAPSE",
)
_SOURCE_TRIGGERED_BY = (
    "script:deactivate_brand_suppliers",
    "script:collapse_provisional_brand_suppliers",
)

# Tolerancia entre deactivated_at y el created_at de la traza para atribuir la
# baja al cleanup: dentro de la misma transacción Postgres now() es constante,
# así que en la práctica son idénticos — 60s cubre relojes/redondeos.
_TRACE_MATCH_TOLERANCE_S = 60

_TAG = "TAG"
_SKIP_ACTIVE = "SKIP_ACTIVE"
_SKIP_ALREADY_FLAGGED = "SKIP_ALREADY_FLAGGED"
_SKIP_REDEACTIVATED = "SKIP_REDEACTIVATED"
_NOT_FOUND = "NOT_FOUND"


async def _plan_tenant(session: AsyncSession, tid: uuid.UUID) -> list[dict[str, Any]]:
    """Decide, sin escribir, qué suppliers de la traza del cleanup hay que taggear."""
    traced = (
        await session.execute(
            text(
                "SELECT decision_data->>'supplier_id' AS sid, "
                "decision_type, max(created_at) AS traced_at "
                "FROM decision_audit_log "
                "WHERE tenant_id = :tid "
                "AND decision_type = ANY(:dts) "
                "AND triggered_by = ANY(:tbs) "
                "AND decision_data->>'supplier_id' IS NOT NULL "
                "GROUP BY 1, 2"
            ),
            {
                "tid": tid,
                "dts": list(_SOURCE_DECISION_TYPES),
                "tbs": list(_SOURCE_TRIGGERED_BY),
            },
        )
    ).mappings().all()

    # Un mismo supplier puede tener traza en ambos decision_type: agrupar.
    by_sid: dict[str, list[Any]] = {}
    for t in traced:
        by_sid.setdefault(t["sid"], []).append(t)

    rows: list[dict[str, Any]] = []
    for sid, traces in by_sid.items():
        sup = (
            await session.execute(
                text(
                    "SELECT name, deactivated_at, custom_fields FROM suppliers "
                    "WHERE id = CAST(:sid AS uuid) AND tenant_id = :tid"
                ),
                {"sid": sid, "tid": tid},
            )
        ).mappings().first()

        min_delta = None
        deactivated_at = None
        if sup is None:
            reason, name = _NOT_FOUND, None
        else:
            name = sup["name"]
            cf = sup["custom_fields"]
            if isinstance(cf, str):
                cf = json.loads(cf)
            cf = cf or {}
            deactivated_at = sup["deactivated_at"]
            # ¿La baja actual es LA del cleanup? Dentro de esa transacción now()
            # fue constante, así que deactivated_at == created_at de la traza.
            # Si difiere, el supplier fue restaurado y vuelto a dar de baja por
            # un humano — baja de negocio real, NO se taggea.
            deltas = (
                [
                    abs((deactivated_at - t["traced_at"]).total_seconds())
                    for t in traces
                ]
                if deactivated_at is not None
                else []
            )
            min_delta = min(deltas) if deltas else None
            matches_trace = min_delta is not None and min_delta <= _TRACE_MATCH_TOLERANCE_S
            if deactivated_at is None:
                # Restaurado/reactivado a propósito (revert o humano): no re-ocultar.
                reason = _SKIP_ACTIVE
            elif is_flag_true(cf.get(BRAND_COLLAPSED_FLAG_KEY)):
                reason = _SKIP_ALREADY_FLAGGED
            elif not matches_trace:
                reason = _SKIP_REDEACTIVATED
            else:
                # Ojo: '_provisional_from_brand' NO exime — las 49 de don pedro
                # nacieron provisionales y después las colapsó el cleanup.
                reason = _TAG

        rows.append(
            {
                "tenant_id": str(tid),
                "supplier_id": sid,
                "supplier_name": name,
                "source_decision_type": ",".join(sorted({t["decision_type"] for t in traces})),
                "reason": reason,
                # Diagnóstico del match temporal (por qué TAG o SKIP_REDEACTIVATED).
                "deactivated_at": str(deactivated_at) if deactivated_at else None,
                "traced_at": ",".join(str(t["traced_at"]) for t in traces),
                "delta_seconds": round(min_delta) if min_delta is not None else None,
            }
        )
    return rows


async def _apply_tenant(
    session: AsyncSession, tid: uuid.UUID, plan: list[dict[str, Any]]
) -> int:
    """Taggea los TAG. Auditado y reversible (ver docstring)."""
    applied = 0
    for r in plan:
        if r["reason"] != _TAG:
            continue
        # ``AND deactivated_at IS NOT NULL`` repite la condición del plan en el
        # WHERE: si alguien reactivó el supplier entre el plan y el apply, no se
        # taggea (quedaría oculto estando activo).
        result = await session.execute(
            text(
                f"UPDATE suppliers SET {SQL_SET_BRAND_COLLAPSED} "
                "WHERE id = CAST(:sid AS uuid) AND tenant_id = :tid "
                "AND deactivated_at IS NOT NULL"
            ),
            {"sid": r["supplier_id"], "tid": tid},
        )
        if getattr(result, "rowcount", 0) == 0:  # reactivado en el medio → skip
            continue
        await insert_decision_audit(
            session,
            tenant_id=str(tid),
            decision_type=_DECISION_TYPE,
            decision_data={
                "supplier_id": r["supplier_id"],
                "supplier_name": r["supplier_name"],
                "source_decision_type": r["source_decision_type"],
                "before": {"_brand_collapsed": None},
                "after": {"_brand_collapsed": "true"},
            },
            triggered_by="script:backfill_brand_collapsed_flag",
        )
        applied += 1
    return applied


def _print_summary(tid: uuid.UUID, plan: list[dict[str, Any]]) -> None:
    from collections import Counter

    by_reason = Counter(r["reason"] for r in plan)
    print(f"tenant {tid}: {len(plan)} supplier(s) en la traza del cleanup — {dict(by_reason)}")
    # Muestra de diagnóstico del match temporal (primeros 3 SKIP_REDEACTIVATED):
    # si el delta es ~horas constantes → problema de timezone; si es días → hubo
    # de verdad otra baja posterior a la traza.
    samples = [r for r in plan if r["reason"] == _SKIP_REDEACTIVATED][:3]
    for r in samples:
        print(
            f"    · {r['supplier_name']!r}: deactivated_at={r['deactivated_at']} "
            f"traza={r['traced_at']} delta={r['delta_seconds']}s"
        )


def _write_report(path: str, rows: list[dict[str, Any]]) -> None:
    if path.lower().endswith(".csv"):
        with open(path, "w", newline="", encoding="utf-8-sig") as fh:
            writer = csv.DictWriter(
                fh,
                fieldnames=[
                    "tenant_id",
                    "supplier_id",
                    "supplier_name",
                    "source_decision_type",
                    "reason",
                    "deactivated_at",
                    "traced_at",
                    "delta_seconds",
                ],
            )
            writer.writeheader()
            writer.writerows(rows)
    else:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(rows, fh, ensure_ascii=False, indent=2)
    print(f"\nReporte escrito en {path} ({len(rows)} fila(s)).")


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
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
        print(f"[{mode}] backfill _brand_collapsed en suppliers de {len(tids)} tenant(s)\n")

        all_rows: list[dict[str, Any]] = []
        total_applied = 0
        for tid in tids:
            plan = await _plan_tenant(session, tid)
            all_rows.extend(plan)
            _print_summary(tid, plan)
            if args.apply:
                total_applied += await _apply_tenant(session, tid, plan)
            print()

        to_tag = sum(1 for r in all_rows if r["reason"] == _TAG)
        print(f"TOTAL: {to_tag} a taggear | {len(all_rows) - to_tag} skip/not-found")

        if args.out and all_rows:
            _write_report(args.out, all_rows)

        if args.apply:
            await session.commit()
            print(
                f"COMMIT: {total_applied} supplier(s) taggeados "
                f"(decision_type={_DECISION_TYPE}). Reversible: ver docstring."
            )
        else:
            await session.rollback()
            print(f"Dry-run: nada se escribió. {to_tag} se taggearían con --apply.")
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
