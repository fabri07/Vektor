"""F-ID.4 — volcar identidad ya existente a `entity_identifiers`.

`entity_identifiers` (capa 3 de F-ID) recién existe desde F-ID.2 — todo lo
que un tenant ya tenía cargado (SKU, DNI/CUIT/CUIL, alias de producto) vive
sólo en las columnas de siempre, no en la tabla transversal. Sin este
bootstrap, el resolvedor (F-ID.3) arrancaría en blanco: sólo tendría datos de
lo que se cargue DE ACÁ EN MÁS, no de lo que el tenant ya trajo. Este script
no genera nada nuevo — sólo copia lo que ya está, con su procedencia real.

## Qué copia cada entidad

**Producto** — `sku` (namespace `"vektor"` si `custom_fields["_sku_origin"]
== "vektor"`, si no `"business"`), `barcode` (siempre `"business"`: un
código de barras nunca lo genera Véktor) y cada alias en
`custom_fields["_aliases"]` (`identifier_type="alias"`, `origin=
"user_confirmed"` — es una decisión humana ya tomada, ver
`domain/product_alias.py`).

**Cliente** — `dni` y `cuit`, cada uno como su propio `identifier_type`,
namespace `"business"`.

**Proveedor** — `cuit` y `cuil`, mismo criterio.

Nunca toca las columnas originales — sólo LEE de ellas y ESCRIBE en
`entity_identifiers`. Idempotente por diseño: `record_identifier` no
duplica un valor ya registrado para la MISMA entidad (sólo refresca
`last_seen_at`).

## Conflictos, no se silencian

Si el mismo valor normalizado ya está registrado para OTRA entidad —posible
en datos históricos que nunca pasaron por la unicidad de F-ID— la fila NO se
pierde en silencio: se cuenta como `CONFLICTO` y queda en el detalle con las
dos entidades involucradas, para revisión humana. El resto del backfill
sigue corriendo.

Usage:
    DATABASE_URL='postgresql://...' .venv/bin/python \
        scripts/bootstrap_entity_identifiers.py --tenant <uuid>

    ... scripts/bootstrap_entity_identifiers.py --all-active --out identidad.csv

    ... scripts/bootstrap_entity_identifiers.py --tenant <uuid> --apply

REVERSIBLE: no hay UPDATE que deshacer (las columnas originales no se
tocan). Para deshacer el volcado, borrar las filas de `entity_identifiers`
con `origin != 'vektor'` y `first_seen_at` posterior a la corrida — no hay
un `decision_audit_log` por fila (sería miles de filas de ruido por tenant),
sólo un resumen por tenant/entidad al final de la corrida con `--apply`.

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
from collections import Counter
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _db import async_engine_config, insert_decision_audit  # noqa: E402
from sqlalchemy import select, text  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine  # noqa: E402

from app.application.services.entity_code_service import (  # noqa: E402
    EntityIdentifierConflictError,
    record_identifier,
)
from app.domain.entity_code import EntityKind  # noqa: E402
from app.domain.product_alias import product_aliases  # noqa: E402
from app.persistence.models.customer import Customer  # noqa: E402
from app.persistence.models.product import Product  # noqa: E402
from app.persistence.models.supplier import Supplier  # noqa: E402

_COPIADO = "COPIADO"
_CONFLICTO = "CONFLICTO"

_DECISION_TYPE = "ENTITY_IDENTIFIER_BOOTSTRAP_SUMMARY"
_TRIGGERED_BY = "script:bootstrap_entity_identifiers"


async def _tenants(session: AsyncSession, args: argparse.Namespace) -> list[str]:
    if args.tenant:
        return [args.tenant]
    rows = await session.execute(
        text("SELECT tenant_id FROM tenants WHERE status = 'ACTIVE' ORDER BY tenant_id")
    )
    return [str(r[0]) for r in rows]


async def _copiar(
    session: AsyncSession,
    conteo: Counter[str],
    detalle: list[dict[str, Any]],
    *,
    tenant_id: str,
    tid: uuid.UUID,
    entity_type: EntityKind,
    entity_id: uuid.UUID,
    name: str,
    identifier_type: str,
    namespace: str,
    raw_value: str | None,
    origin: str,
    apply: bool,
) -> None:
    if not raw_value or not raw_value.strip():
        return
    if not apply:
        conteo[_COPIADO] += 1
        detalle.append(
            {
                "tenant_id": tenant_id,
                "entity_type": entity_type,
                "entity_id": str(entity_id),
                "name": name,
                "identifier_type": identifier_type,
                "namespace": namespace,
                "valor": raw_value,
                "estado": _COPIADO,
            }
        )
        return
    try:
        await record_identifier(
            session, tid, entity_type, entity_id, identifier_type, namespace, raw_value, origin
        )
    except EntityIdentifierConflictError as conflict:
        conteo[_CONFLICTO] += 1
        detalle.append(
            {
                "tenant_id": tenant_id,
                "entity_type": entity_type,
                "entity_id": str(entity_id),
                "name": name,
                "identifier_type": identifier_type,
                "namespace": namespace,
                "valor": raw_value,
                "estado": f"{_CONFLICTO} (ya pertenece a {conflict.existing_entity_id})",
            }
        )
        return
    conteo[_COPIADO] += 1
    detalle.append(
        {
            "tenant_id": tenant_id,
            "entity_type": entity_type,
            "entity_id": str(entity_id),
            "name": name,
            "identifier_type": identifier_type,
            "namespace": namespace,
            "valor": raw_value,
            "estado": _COPIADO,
        }
    )


async def _procesar_productos(
    session: AsyncSession, tenant_id: str, *, apply: bool
) -> tuple[Counter[str], list[dict[str, Any]]]:
    conteo: Counter[str] = Counter()
    detalle: list[dict[str, Any]] = []
    tid = uuid.UUID(tenant_id)

    rows = (
        await session.execute(
            select(Product).where(Product.tenant_id == tid, Product.is_active.is_(True))
        )
    ).scalars().all()

    for product in rows:
        sku_origin = (
            "vektor" if (product.custom_fields or {}).get("_sku_origin") == "vektor" else "business"
        )
        await _copiar(
            session, conteo, detalle,
            tenant_id=tenant_id, tid=tid, entity_type="product", entity_id=product.id,
            name=product.name, identifier_type="sku", namespace=sku_origin,
            raw_value=product.sku, origin=sku_origin, apply=apply,
        )
        await _copiar(
            session, conteo, detalle,
            tenant_id=tenant_id, tid=tid, entity_type="product", entity_id=product.id,
            name=product.name, identifier_type="barcode", namespace="business",
            raw_value=product.barcode, origin="business", apply=apply,
        )
        for alias in product_aliases(product.custom_fields):
            await _copiar(
                session, conteo, detalle,
                tenant_id=tenant_id, tid=tid, entity_type="product", entity_id=product.id,
                name=product.name, identifier_type="alias", namespace="business",
                raw_value=alias, origin="user_confirmed", apply=apply,
            )
    return conteo, detalle


async def _procesar_clientes(
    session: AsyncSession, tenant_id: str, *, apply: bool
) -> tuple[Counter[str], list[dict[str, Any]]]:
    conteo: Counter[str] = Counter()
    detalle: list[dict[str, Any]] = []
    tid = uuid.UUID(tenant_id)

    rows = (
        await session.execute(select(Customer).where(Customer.tenant_id == tid))
    ).scalars().all()

    for customer in rows:
        if customer.is_sentinel:
            continue
        for identifier_type, raw_value in (("dni", customer.dni), ("cuit", customer.cuit)):
            await _copiar(
                session, conteo, detalle,
                tenant_id=tenant_id, tid=tid, entity_type="customer", entity_id=customer.id,
                name=customer.name, identifier_type=identifier_type, namespace="business",
                raw_value=raw_value, origin="business", apply=apply,
            )
    return conteo, detalle


async def _procesar_proveedores(
    session: AsyncSession, tenant_id: str, *, apply: bool
) -> tuple[Counter[str], list[dict[str, Any]]]:
    conteo: Counter[str] = Counter()
    detalle: list[dict[str, Any]] = []
    tid = uuid.UUID(tenant_id)

    rows = (
        await session.execute(select(Supplier).where(Supplier.tenant_id == tid))
    ).scalars().all()

    for supplier in rows:
        if supplier.is_sentinel:
            continue
        for identifier_type, raw_value in (("cuit", supplier.cuit), ("cuil", supplier.cuil)):
            await _copiar(
                session, conteo, detalle,
                tenant_id=tenant_id, tid=tid, entity_type="supplier", entity_id=supplier.id,
                name=supplier.name, identifier_type=identifier_type, namespace="business",
                raw_value=raw_value, origin="business", apply=apply,
            )
    return conteo, detalle


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant", help="UUID del tenant")
    parser.add_argument("--all-active", action="store_true", help="todos los ACTIVE")
    parser.add_argument("--apply", action="store_true", help="escribe (default: dry-run)")
    parser.add_argument("--out", help="detalle a CSV (.csv) o JSON")
    args = parser.parse_args()

    if not args.tenant and not args.all_active:
        print("ERROR: pasá --tenant <uuid> o --all-active.")
        sys.exit(2)

    url, connect_args = async_engine_config()
    engine = create_async_engine(url, connect_args=connect_args)
    total: Counter[str] = Counter()
    detalle: list[dict[str, Any]] = []
    try:
        async with AsyncSession(engine) as session:
            for tid_str in await _tenants(session, args):
                por_tenant: Counter[str] = Counter()
                for fn in (_procesar_productos, _procesar_clientes, _procesar_proveedores):
                    conteo, filas = await fn(session, tid_str, apply=args.apply)
                    por_tenant.update(conteo)
                    detalle.extend(filas)
                total.update(por_tenant)
                if args.apply and sum(por_tenant.values()):
                    await insert_decision_audit(
                        session,
                        tenant_id=tid_str,
                        decision_type=_DECISION_TYPE,
                        decision_data={
                            "copiado": por_tenant.get(_COPIADO, 0),
                            "conflicto": por_tenant.get(_CONFLICTO, 0),
                        },
                        triggered_by=_TRIGGERED_BY,
                    )
            if args.apply:
                await session.commit()
    finally:
        await engine.dispose()

    modo = "APLICADO" if args.apply else "DRY-RUN (nada se escribió)"
    revisados = sum(total.values())
    print(f"\n{'=' * 70}\n  BOOTSTRAP DE IDENTIFICADORES — {modo}\n{'=' * 70}")
    print(f"  identificadores revisados: {revisados}")
    for estado in (_COPIADO, _CONFLICTO):
        n = total.get(estado, 0)
        pct = 100 * n // revisados if revisados else 0
        print(f"    {estado:<12} {n:>6}  ({pct:>3}%)")
    if total.get(_CONFLICTO):
        print(
            "\n  CONFLICTO: el mismo valor ya está registrado para OTRA entidad —\n"
            "  dato histórico que nunca pasó por la unicidad de F-ID. No se pierde:\n"
            "  queda en el detalle (--out) con las dos entidades involucradas, para\n"
            "  revisión humana. Este script nunca decide solo cuál es la correcta."
        )

    if args.out:
        fieldnames = [
            "tenant_id", "entity_type", "entity_id", "name",
            "identifier_type", "namespace", "valor", "estado",
        ]
        if args.out.endswith(".csv"):
            with open(args.out, "w", newline="", encoding="utf-8") as fh:
                writer = csv.DictWriter(fh, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(detalle)
        else:
            with open(args.out, "w", encoding="utf-8") as fh:
                json.dump(detalle, fh, ensure_ascii=False, indent=2)
        print(f"\n  detalle escrito: {args.out}  ({len(detalle)} filas)")


if __name__ == "__main__":
    asyncio.run(main())
