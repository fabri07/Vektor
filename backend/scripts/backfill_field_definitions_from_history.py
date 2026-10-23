"""Recupera visibilidad de claves de negocio guardadas en `custom_fields` sin
definición — Cambio 1 (información histórica) del plan de conservación y
acceso a datos de negocio (docs/plans/conservacion-y-acceso-datos-negocio.md).

Qué problema resuelve
---------------------
Una clave puede estar en `custom_fields` de una fila real (ej. `marca` en un
producto de la Reforma de Proveedores, o `col_8` — el encabezado sin nombre
que el import preserva) sin que exista una `TenantCustomFieldDefinition` que
la describa. El dato está guardado y es correcto; lo que falta es la
metadata que lo hace aparecer en un selector de columnas o en una
exportación. Este script SOLO agrega esa metadata — nunca toca los valores.

Qué NO hace
-----------
No reimporta nada, no modifica `custom_fields` de ninguna fila, no renombra ni
reactiva una definición ya existente (`ensure_custom_field_exists` ya protege
eso — este script reusa el mismo criterio: si ya hay definición, no se toca).
Una clave reservada (`RESERVED_KEYS`) NUNCA se publica, aunque tenga datos
reales — es evidencia interna (marcadores de sistema, campos derivados de otra
UI) y exponerla confundiría al usuario o filtraría detalle de implementación.

Alcance de esta entrega: solo `product`. Extender a sale/expense/customer/
supplier es la continuación natural (mismo patrón), pero requiere primero
revisar sus claves reales — este script no adivina qué es seguro para una
entidad que no se auditó.

RESULTADOS (contados y reportados en --out):
    CREATE         → clave de negocio sin definición → se crea (solo --apply).
    SKIP_DEFINED   → ya tiene TenantCustomFieldDefinition (declarada o de una
                     corrida anterior de este mismo script) → idempotencia.
    SKIP_STATIC    → la cubre el catálogo estático (business_field_catalog.py,
                     ej. purchase_base_cost) → tiene identidad, no hace falta
                     una fila en la tabla.
    SKIP_RESERVED  → clave técnica/interna (RESERVED_KEYS) → nunca se publica.

Usage:
    # Dry-run (default) de un tenant puntual:
    DATABASE_URL='postgresql://...' .venv/bin/python \
        scripts/backfill_field_definitions_from_history.py --tenant <uuid>

    # Dry-run global:
    ... scripts/backfill_field_definitions_from_history.py --all-active

    # Reporte detallado:
    ... scripts/backfill_field_definitions_from_history.py --all-active --out reporte.csv

    # Aplicar (después de revisar el dry-run):
    ... scripts/backfill_field_definitions_from_history.py --all-active --apply

REVERSIBLE: cada creación queda en `tenant_field_change_log` (action="created",
previous_state=null) — el undo existente (`POST /fields/{key}/undo`) no aplica
a una creación sin estado previo; borrar a mano por `field_key` si hace falta:

    DELETE FROM tenant_custom_field_definitions
      WHERE tenant_id = '<tid>' AND entity_type = 'product'
        AND field_key = ANY(:keys_creadas_por_esta_corrida);

NUNCA imprime la connection URL. Read-only por default. Correr desde backend/.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import uuid
from datetime import UTC, datetime
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _db import async_engine_config  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine  # noqa: E402

from app.domain.business_field_catalog import descriptors_for  # noqa: E402

#: Tabla por entidad. Solo `product` está cubierta hoy — agregar una entidad acá
#: sin auditar sus claves reales sería adivinar, no recuperar.
_TABLE_BY_ENTITY: dict[str, str] = {
    "product": "products",
}

#: Claves técnicas/internas verificadas contra datos reales (ASTERIA
#: 2026-09-12, 398 productos) — nunca se publican como campo de negocio.
#: Ampliar esta lista es SEGURO (solo excluye más); achicarla sin volver a
#: verificar los datos reales no lo es.
RESERVED_KEYS: dict[str, frozenset[str]] = {
    "product": frozenset(
        {
            # Centinelas y flags de sistema (ver models/_sentinel.py, supplier.py).
            "_sentinel",
            "_brand_collapsed",
            "_provisional_from_brand",
            # F-H6.d: procedencia del costo, restaurada por F11 — es metadata
            # de OTRO campo (unit_cost_ars), no un dato propio.
            "_vektor_costo_base",
            # E6a-B: código externo demasiado largo para persistir — cola de
            # revisión manual, no un campo del producto.
            "_external_code_pendiente_revision",
            # Bloque 3B: evidencia de la sugerencia de categoría — ya visible
            # vía `category` + esta metadata es para debug del clasificador,
            # no para que el usuario la edite como si fuera un dato propio.
            "category_suggestion_code",
            "category_suggestion_confidence",
            "category_suggestion_evidence",
            "category_suggestion_rule",
            # Label libre de una categoría "OTHER" — ya se lee en el frontend
            # (`categoryDisplay`) junto a `category`, no es un campo aparte.
            "category_label",
        }
    ),
}

_CREATE = "CREATE"
_SKIP_DEFINED = "SKIP_DEFINED"
_SKIP_STATIC = "SKIP_STATIC"
_SKIP_RESERVED = "SKIP_RESERVED"


def _slug_a_label(field_key: str) -> str:
    """Label legible por default — el usuario puede renombrarlo después
    (`PATCH /fields/{key}`); esto es solo para que no aparezca en blanco."""
    return field_key.replace("_", " ").strip().capitalize() or field_key


async def _claves_reales(session: AsyncSession, tenant_id: uuid.UUID, table: str) -> set[str]:
    """Claves top-level de `custom_fields` presentes en AL MENOS una fila viva
    del tenant. `jsonb_object_keys` sobre un agregado evita traer las filas."""
    rows = await session.execute(
        text(
            f"SELECT DISTINCT jsonb_object_keys(custom_fields) AS k "
            f"FROM {table} "
            f"WHERE tenant_id = :tid AND custom_fields IS NOT NULL "
            f"AND custom_fields != '{{}}'::jsonb"
        ),
        {"tid": str(tenant_id)},
    )
    return {r[0] for r in rows.all()}


async def _claves_ya_definidas(
    session: AsyncSession, tenant_id: uuid.UUID, entity_type: str
) -> set[str]:
    rows = await session.execute(
        text(
            "SELECT field_key FROM tenant_custom_field_definitions "
            "WHERE tenant_id = :tid AND entity_type = :et"
        ),
        {"tid": str(tenant_id), "et": entity_type},
    )
    return {r[0] for r in rows.all()}


async def _plan_tenant(
    session: AsyncSession, tenant_id: uuid.UUID, entity_type: str
) -> list[dict[str, Any]]:
    table = _TABLE_BY_ENTITY[entity_type]
    reservadas = RESERVED_KEYS.get(entity_type, frozenset())
    estaticas = {d.field_key for d in descriptors_for(entity_type)}

    presentes = await _claves_reales(session, tenant_id, table)
    ya_definidas = await _claves_ya_definidas(session, tenant_id, entity_type)

    plan: list[dict[str, Any]] = []
    for key in sorted(presentes):
        if key in reservadas:
            reason = _SKIP_RESERVED
        elif key in estaticas:
            reason = _SKIP_STATIC
        elif key in ya_definidas:
            reason = _SKIP_DEFINED
        else:
            reason = _CREATE
        plan.append({"tenant_id": str(tenant_id), "entity_type": entity_type, "field_key": key,
                      "reason": reason})
    return plan


async def _apply_tenant(session: AsyncSession, plan: list[dict[str, Any]]) -> int:
    now = datetime.now(UTC)
    aplicados = 0
    for row in plan:
        if row["reason"] != _CREATE:
            continue
        field_id = uuid.uuid4()
        await session.execute(
            text(
                "INSERT INTO tenant_custom_field_definitions "
                "(id, tenant_id, entity_type, field_key, override_label, data_type, "
                " is_enabled, is_base_field, display_order, created_at, updated_at) "
                "VALUES (:id, CAST(:tid AS uuid), :et, :fk, :label, 'text', "
                " true, false, 0, :now, :now)"
            ),
            {
                "id": str(field_id),
                "tid": row["tenant_id"],
                "et": row["entity_type"],
                "fk": row["field_key"],
                "label": _slug_a_label(row["field_key"]),
                "now": now,
            },
        )
        await session.execute(
            text(
                "INSERT INTO tenant_field_change_log "
                "(id, tenant_id, field_key, entity_type, action, previous_state, "
                " new_state, changed_by, changed_at) "
                "VALUES (:id, CAST(:tid AS uuid), :fk, :et, 'created', NULL, "
                " CAST(:new_state AS jsonb), NULL, :now)"
            ),
            {
                "id": str(uuid.uuid4()),
                "tid": row["tenant_id"],
                "fk": row["field_key"],
                "et": row["entity_type"],
                "new_state": json.dumps(
                    {
                        "field_key": row["field_key"],
                        "entity_type": row["entity_type"],
                        "data_type": "text",
                        "override_label": _slug_a_label(row["field_key"]),
                        "is_enabled": True,
                        "display_order": 0,
                        "source": "backfill_field_definitions_from_history",
                    }
                ),
                "now": now,
            },
        )
        aplicados += 1
    return aplicados


def _print_summary(tenant_id: uuid.UUID, plan: list[dict[str, Any]]) -> None:
    by_reason: dict[str, int] = {}
    for row in plan:
        by_reason[row["reason"]] = by_reason.get(row["reason"], 0) + 1
    detalle = "  ".join(f"{k}={v}" for k, v in sorted(by_reason.items()))
    a_crear = [r["field_key"] for r in plan if r["reason"] == _CREATE]
    print(f"  {tenant_id}  {detalle or '(sin custom_fields)'}")
    if a_crear:
        print(f"    a crear: {', '.join(a_crear)}")


def _write_report(path: str, rows: list[dict[str, Any]]) -> None:
    if path.endswith(".csv"):
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
    else:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(rows, f, indent=2, default=str)
    print(f"Reporte escrito en {path}")


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant", help="UUID de tenant puntual")
    parser.add_argument("--all-active", action="store_true", help="Todos los tenants activos")
    parser.add_argument(
        "--entity", default="product", choices=sorted(_TABLE_BY_ENTITY), help="Entidad a revisar"
    )
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
        print(f"[{mode}] recuperar definiciones de '{args.entity}' en {len(tids)} tenant(s)\n")

        all_rows: list[dict[str, Any]] = []
        total_creados = 0
        for tid in tids:
            plan = await _plan_tenant(session, tid, args.entity)
            all_rows.extend(plan)
            if plan:
                _print_summary(tid, plan)
            if args.apply:
                total_creados += await _apply_tenant(session, plan)

        a_crear = sum(1 for r in all_rows if r["reason"] == _CREATE)
        print(f"\nTOTAL: {a_crear} definición(es) a crear | {len(all_rows) - a_crear} skip")

        if args.out and all_rows:
            _write_report(args.out, all_rows)

        if args.apply:
            await session.commit()
            print(f"COMMIT: {total_creados} definición(es) creada(s). Reversible: ver docstring.")
        else:
            await session.rollback()
            print(f"Dry-run: nada se escribió. {a_crear} se crearían con --apply.")
    await engine.dispose()


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
