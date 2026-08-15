"""F-ID.6 — código Véktor para producto/cliente/proveedor que nacieron sin uno.

Desde F-ID.5 toda alta NUEVA nace con código (`sku` para producto,
`vektor_code` para cliente/proveedor). Este script es para lo que ya
existía antes de esa fase — mismo patrón que ``backfill_product_category.py``
(dry-run/--apply, auditado, cobertura antes que "cuántos toqué").

## Qué hace y qué NO hace por entidad

**Producto** — asigna sobre `sku IS NULL`. Reusa `assign_vektor_code_if_missing`
(mismo prefijo por categoría/vertical que F-ID.5, mismo `_sku_origin=vektor`).
**No corre dedup**: asume que `scripts/dedupe_products_by_name.py --apply` ya
corrió. Correrlo después de este script no rompe nada (el índice único lo
garantiza), sólo desperdicia números de secuencia en productos que se
fusionan — ver la nota en `_GATE_NOTE` más abajo, mismo criterio de
honestidad que `20260813_0001` explicando por qué NO forzó unicidad de CUIT.

**Cliente** — asigna sobre `vektor_code IS NULL`, excluyendo sentinela y
soft-deleted. **Sin gate de duplicados**: `dni`/`cuit` ya son únicos por
índice desde `20260721_0001`, y F-E ya decidió que el nombre nunca es
identidad — no hay ambigüedad real que detectar acá.

**Proveedor** — asigna sobre `vektor_code IS NULL`, excluyendo sentinela y
marca-colapsada. **Con gate nuevo, que NO bloquea la numeración**: agrupa
TODOS los proveedores activos (no sólo los que están sin código) por
`normalize_text(name)`; un grupo con más de un proveedor se reporta como
`POSIBLE_DUPLICADO` (visible, auditado) — pero cada uno de sus miembros
IGUAL recibe su código si le faltaba. La fusión, si corresponde, es una
decisión humana aparte (fuera de alcance: reabriría la Reforma de
Proveedores).

## Cobertura, no sólo cambios

    ASIGNADO           → no tenía código, se le dio uno (con --apply).
    YA_TENIA           → ya tenía código: no se toca (idempotencia).
    POSIBLE_DUPLICADO  → (sólo proveedor) coincide en nombre normalizado con
                          otro proveedor activo — igual recibe código, pero
                          queda marcado para revisión humana.

Usage:
    DATABASE_URL='postgresql://...' .venv/bin/python \
        scripts/backfill_entity_code.py --entity product --tenant <uuid>

    ... scripts/backfill_entity_code.py --entity customer --all-active --out clientes.csv

    ... scripts/backfill_entity_code.py --entity supplier --tenant <uuid> --apply

REVERSIBLE: cada asignación queda auditada
(``decision_type='ENTITY_CODE_BACKFILL'``, con entity_id + código + entity_type).
Undo en bloque (ejemplo para cliente):

    UPDATE customers SET vektor_code = NULL, vektor_code_normalized = NULL
      WHERE id IN (
        SELECT (decision_data->>'entity_id')::uuid
        FROM decision_audit_log
        WHERE tenant_id = '<tid>' AND decision_type = 'ENTITY_CODE_BACKFILL'
          AND decision_data->>'entity_type' = 'customer'
      );

(No revierte la fila de ``entity_identifiers`` a propósito — es insert-only
por diseño, ver el docstring de ``persistence/models/entity_identifier.py``.)

NUNCA imprime la connection URL. Read-only por default: sin ``--apply``, nunca
llama a la asignación real (que toma el lock de fila de
``assign_next_sequence`` y lo sostiene hasta el cierre de la sesión) — sólo
evalúa con ``needs_vektor_code`` si asignaría, sin escribir ni lockear nada.
Correr desde backend/.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import sys
import uuid
from collections import Counter, defaultdict
from typing import Any, TypeVar

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _db import async_engine_config, insert_decision_audit  # noqa: E402
from sqlalchemy import select, text  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine  # noqa: E402

from app.application.services.entity_code_service import (  # noqa: E402
    CUSTOMER_CODE_SPEC,
    PRODUCT_CODE_SPEC,
    SUPPLIER_CODE_SPEC,
    EntityCodeSpec,
    assign_vektor_code_if_missing,
    needs_vektor_code,
)
from app.domain.text_norm import normalize_text  # noqa: E402
from app.domain.verticals import Vertical, parse_vertical  # noqa: E402
from app.persistence.models.customer import Customer  # noqa: E402
from app.persistence.models.product import Product  # noqa: E402
from app.persistence.models.supplier import Supplier  # noqa: E402

_M = TypeVar("_M", Customer, Supplier)

_ASIGNADO = "ASIGNADO"
_YA_TENIA = "YA_TENIA"
_POSIBLE_DUPLICADO = "POSIBLE_DUPLICADO"

_DECISION_TYPE = "ENTITY_CODE_BACKFILL"
_TRIGGERED_BY = "script:backfill_entity_code"

_GATE_NOTE = (
    "Este script asume que scripts/dedupe_products_by_name.py --apply ya corrió "
    "sobre el tenant (sólo aplica a --entity product). Correrlo antes evita "
    "desperdiciar números de secuencia en productos que van a fusionarse; no "
    "correrlo no rompe nada (el índice único de sku sigue siendo la garantía "
    "dura), sólo dejaría huecos en la numeración de más."
)


async def _tenants(session: AsyncSession, args: argparse.Namespace) -> list[str]:
    if args.tenant:
        return [args.tenant]
    rows = await session.execute(
        text("SELECT tenant_id FROM tenants WHERE status = 'ACTIVE' ORDER BY tenant_id")
    )
    return [str(r[0]) for r in rows]


async def _vertical_de(session: AsyncSession, tenant_id: str) -> Vertical | None:
    row = (
        await session.execute(
            text(
                "SELECT vertical_code FROM business_profiles "
                "WHERE tenant_id = CAST(:tid AS uuid) LIMIT 1"
            ),
            {"tid": tenant_id},
        )
    ).first()
    if row is None or not row[0]:
        return None
    try:
        return parse_vertical(row[0])
    except Exception:  # noqa: BLE001
        return None


async def _procesar_productos(
    session: AsyncSession, tenant_id: str, *, apply: bool
) -> tuple[Counter[str], list[dict[str, Any]]]:
    conteo: Counter[str] = Counter()
    detalle: list[dict[str, Any]] = []
    vertical = await _vertical_de(session, tenant_id)
    tid = uuid.UUID(tenant_id)

    rows = (
        await session.execute(
            select(Product)
            .where(
                Product.tenant_id == tid,
                Product.is_active.is_(True),
                Product.deactivated_at.is_(None),
            )
            .order_by(Product.created_at)
        )
    ).scalars().all()

    for product in rows:
        had_code = bool(product.sku)
        if apply:
            assigned = await assign_vektor_code_if_missing(
                session,
                product,
                PRODUCT_CODE_SPEC,
                product.tenant_id,
                vertical=vertical,
                category=product.category,
            )
        else:
            # Dry-run: NUNCA llama a la asignación real — eso toma el lock de
            # fila de `assign_next_sequence` y lo mantiene hasta que cierra la
            # sesión (todo el scan). Sólo evalúa si asignaría, sin escribir nada.
            assigned = needs_vektor_code(product, PRODUCT_CODE_SPEC)
        estado = _ASIGNADO if assigned else _YA_TENIA
        conteo[estado] += 1
        detalle.append(
            {
                "tenant_id": tenant_id,
                "entity_type": "product",
                "entity_id": str(product.id),
                "name": product.name,
                "estado": estado,
                "codigo": product.sku or "",
            }
        )
        if assigned and apply:
            await insert_decision_audit(
                session,
                tenant_id=tenant_id,
                decision_type=_DECISION_TYPE,
                decision_data={
                    "entity_type": "product",
                    "entity_id": str(product.id),
                    "name": product.name,
                    "codigo": product.sku,
                    "had_code_before": had_code,
                },
                triggered_by=_TRIGGERED_BY,
            )
    return conteo, detalle


async def _procesar_simple(
    session: AsyncSession,
    tenant_id: str,
    *,
    apply: bool,
    model: type[_M],
    spec: EntityCodeSpec,
    entity_type: str,
) -> tuple[Counter[str], list[dict[str, Any]]]:
    """Cliente/proveedor sin el gate de posible-duplicado (eso sólo aplica a
    proveedor, ver `_procesar_proveedores`)."""
    conteo: Counter[str] = Counter()
    detalle: list[dict[str, Any]] = []
    tid = uuid.UUID(tenant_id)

    rows = (
        await session.execute(
            select(model)
            .where(model.tenant_id == tid, model.deactivated_at.is_(None))
            .order_by(model.created_at)
        )
    ).scalars().all()

    for entity in rows:
        if getattr(entity, "is_sentinel", False):
            continue
        if apply:
            assigned = await assign_vektor_code_if_missing(session, entity, spec, entity.tenant_id)
        else:
            assigned = needs_vektor_code(entity, spec)
        estado = _ASIGNADO if assigned else _YA_TENIA
        conteo[estado] += 1
        detalle.append(
            {
                "tenant_id": tenant_id,
                "entity_type": entity_type,
                "entity_id": str(entity.id),
                "name": entity.name,
                "estado": estado,
                "codigo": entity.vektor_code or "",
            }
        )
        if assigned and apply:
            await insert_decision_audit(
                session,
                tenant_id=tenant_id,
                decision_type=_DECISION_TYPE,
                decision_data={
                    "entity_type": entity_type,
                    "entity_id": str(entity.id),
                    "name": entity.name,
                    "codigo": entity.vektor_code,
                },
                triggered_by=_TRIGGERED_BY,
            )
    return conteo, detalle


async def _procesar_proveedores(
    session: AsyncSession, tenant_id: str, *, apply: bool
) -> tuple[Counter[str], list[dict[str, Any]]]:
    conteo: Counter[str] = Counter()
    detalle: list[dict[str, Any]] = []
    tid = uuid.UUID(tenant_id)

    rows = (
        await session.execute(
            select(Supplier)
            .where(Supplier.tenant_id == tid, Supplier.deactivated_at.is_(None))
            .order_by(Supplier.created_at)
        )
    ).scalars().all()

    activos = [s for s in rows if not s.is_sentinel and not s.is_brand_collapsed]
    grupos: dict[str, list[Supplier]] = defaultdict(list)
    for supplier in activos:
        grupos[normalize_text(supplier.name)].append(supplier)
    duplicados = {norm for norm, miembros in grupos.items() if len(miembros) > 1}

    for supplier in activos:
        es_duplicado = normalize_text(supplier.name) in duplicados
        if apply:
            assigned = await assign_vektor_code_if_missing(
                session, supplier, SUPPLIER_CODE_SPEC, supplier.tenant_id
            )
        else:
            assigned = needs_vektor_code(supplier, SUPPLIER_CODE_SPEC)
        if es_duplicado:
            estado = _POSIBLE_DUPLICADO
        elif assigned:
            estado = _ASIGNADO
        else:
            estado = _YA_TENIA
        conteo[estado] += 1
        detalle.append(
            {
                "tenant_id": tenant_id,
                "entity_type": "supplier",
                "entity_id": str(supplier.id),
                "name": supplier.name,
                "estado": estado,
                "codigo": supplier.vektor_code or "",
            }
        )
        if assigned and apply:
            await insert_decision_audit(
                session,
                tenant_id=tenant_id,
                decision_type=_DECISION_TYPE,
                decision_data={
                    "entity_type": "supplier",
                    "entity_id": str(supplier.id),
                    "name": supplier.name,
                    "codigo": supplier.vektor_code,
                    "posible_duplicado": es_duplicado,
                },
                triggered_by=_TRIGGERED_BY,
            )
    return conteo, detalle


_ESTADOS_POR_ENTIDAD: dict[str, tuple[str, ...]] = {
    "product": (_ASIGNADO, _YA_TENIA),
    "customer": (_ASIGNADO, _YA_TENIA),
    "supplier": (_ASIGNADO, _YA_TENIA, _POSIBLE_DUPLICADO),
}


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--entity", required=True, choices=["product", "customer", "supplier"])
    parser.add_argument("--tenant", help="UUID del tenant")
    parser.add_argument("--all-active", action="store_true", help="todos los ACTIVE")
    parser.add_argument("--apply", action="store_true", help="escribe (default: dry-run)")
    parser.add_argument("--out", help="detalle a CSV (.csv) o JSON")
    args = parser.parse_args()

    if not args.tenant and not args.all_active:
        print("ERROR: pasá --tenant <uuid> o --all-active.")
        sys.exit(2)

    if args.entity == "product":
        print(_GATE_NOTE)

    url, connect_args = async_engine_config()
    engine = create_async_engine(url, connect_args=connect_args)
    total: Counter[str] = Counter()
    detalle: list[dict[str, Any]] = []
    try:
        async with AsyncSession(engine) as session:
            for tid in await _tenants(session, args):
                if args.entity == "product":
                    conteo, filas = await _procesar_productos(session, tid, apply=args.apply)
                elif args.entity == "customer":
                    conteo, filas = await _procesar_simple(
                        session,
                        tid,
                        apply=args.apply,
                        model=Customer,
                        spec=CUSTOMER_CODE_SPEC,
                        entity_type="customer",
                    )
                else:
                    conteo, filas = await _procesar_proveedores(session, tid, apply=args.apply)
                total.update(conteo)
                detalle.extend(filas)
            if args.apply:
                await session.commit()
    finally:
        await engine.dispose()

    modo = "APLICADO" if args.apply else "DRY-RUN (nada se escribió)"
    revisados = sum(total.values())
    print(f"\n{'=' * 70}\n  BACKFILL DE CÓDIGO VÉKTOR ({args.entity}) — {modo}\n{'=' * 70}")
    print(f"  entidades revisadas: {revisados}")
    for estado in _ESTADOS_POR_ENTIDAD[args.entity]:
        n = total.get(estado, 0)
        pct = 100 * n // revisados if revisados else 0
        print(f"    {estado:<18} {n:>6}  ({pct:>3}%)")
    if args.entity == "supplier" and total.get(_POSIBLE_DUPLICADO):
        print(
            "\n  POSIBLE_DUPLICADO no bloquea nada: cada proveedor del grupo recibió\n"
            "  su propio código igual. Es una señal para revisar y fusionar a mano\n"
            "  si corresponde — este script nunca fusiona solo."
        )

    if args.out:
        if args.out.endswith(".csv"):
            with open(args.out, "w", newline="", encoding="utf-8") as fh:
                fieldnames = [
                    "tenant_id",
                    "entity_type",
                    "entity_id",
                    "name",
                    "estado",
                    "codigo",
                ]
                writer = csv.DictWriter(fh, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(detalle)
        else:
            with open(args.out, "w", encoding="utf-8") as fh:
                json.dump(detalle, fh, ensure_ascii=False, indent=2)
        print(f"\n  detalle escrito: {args.out}  ({len(detalle)} filas)")


if __name__ == "__main__":
    asyncio.run(main())
