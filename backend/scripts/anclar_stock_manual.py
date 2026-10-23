"""M·P2c: ancla en el ledger el stock que se cargó a mano sin movimiento.

Hasta M·P2c, el alta y la edición de un producto fijaban ``stock_units`` sin
escribir un movimiento. El costo promedio ve ese stock «fuera del ledger» y deja
el producto en ``sin_medicion`` para siempre. Desde M·P2c la carga manual escribe
su movimiento; este script repara lo que quedó de antes.

Por cada producto con stock fuera del ledger (la misma detección que el costo
promedio, ``costo_promedio_repository.cargar_historias``):

- **diferencia POSITIVA** (``stock_units`` > Σ ledger): escribe una APERTURA por
  la diferencia (``adjustment`` + ``catalog_initial_stock``, la misma que deja un
  catálogo importado en «ya la tenía»), fechada en el alta del producto (día de
  negocio AR). NO toca ``stock_units``: el valor ya está, sólo hace que el ledger
  lo explique.
  - **Costo:** sólo uno que se conozca A ESA FECHA: una compra o un stock inicial
    con fecha ≤ el alta (``costo_de_referencia``), o el costo tipeado en el mismo
    día del alta (la edición manual sellada). El costo de HOY no se usa: con
    inflación, ponerle el precio actual a stock viejo inventa un costo. Sin
    ninguno, la apertura va sin costo y el producto queda ``incompleto``, igual
    que una fila de catálogo sin costo.
- Sólo productos ACTIVOS; toma el lock compartido de mantenimiento del negocio.
- **diferencia NEGATIVA:** no se ancla (no es stock que el negocio tenía); se
  informa.

Límite conocido: si la diferencia vino de ediciones POSTERIORES al alta, anclarla
en el alta adelanta unidades. La proyección lo absorbe con el clamp de siempre y
el producto queda ``aproximado``.

- **Sin ``--apply`` (default) es un DRY-RUN:** no escribe nada.
- **``--apply``:** escribe las aperturas y marca el costo promedio pendiente de
  esos productos (todo el historial). Commitea por negocio. Idempotente: el
  ``source_event_id`` es ``manual:ancla:{product_id}`` y un producto ya anclado
  deja de estar fuera del ledger.

Nunca imprime la connection URL: sólo ``host`` y ``base``.

Usage (desde ``backend/``):
    DATABASE_URL='postgresql://...' .venv/bin/python scripts/anclar_stock_manual.py --all
    ... --tenant-id <uuid> [--apply]
    ... --email x@y.com [--apply]
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from urllib.parse import urlparse

_SCRIPTS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _SCRIPTS)
sys.path.insert(0, os.path.dirname(_SCRIPTS))

from _db import async_engine_config  # noqa: E402
from sqlalchemy import func, select, text  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker  # noqa: E402

from app.application.services import maintenance_lock_service  # noqa: E402
from app.application.services.inventory_movement_origin import (  # noqa: E402
    SOURCE_CATALOG_INITIAL_STOCK,
    momento_de_negocio_rotulado,
)
from app.domain.business_time import a_hora_de_negocio  # noqa: E402
from app.domain.contrato_de_costo import contrato_declarado  # noqa: E402
from app.domain.costo_vigente import CATALOGO_FIELD, MANUAL_FIELD  # noqa: E402
from app.persistence.costo_promedio_pendiente import marcar_todo_el_historial  # noqa: E402
from app.persistence.db.engine_factory import crear_engine  # noqa: E402
from app.persistence.models.inventory import InventoryMovement  # noqa: E402
from app.persistence.models.product import Product  # noqa: E402
from app.persistence.repositories.costo_de_referencia_repository import (  # noqa: E402
    costos_de_referencia,
)
from app.persistence.repositories.costo_promedio_repository import (  # noqa: E402
    cargar_historias,
)

MUESTRA = 10


@dataclass
class Informe:
    anclados: int = 0
    sin_costo: int = 0
    negativos: int = 0
    unidades: int = 0
    muestra: list[str] = field(default_factory=list)
    muestra_negativos: list[str] = field(default_factory=list)


def _costo_tipeado_en_el_alta(cf: dict[str, object] | None, dia_alta: date) -> Decimal | None:
    """El costo tipeado (edición manual o catálogo) el MISMO día del alta."""
    for clave in (MANUAL_FIELD, CATALOGO_FIELD):
        sello = (cf or {}).get(clave)
        if not isinstance(sello, dict):
            continue
        try:
            fecha = datetime.fromisoformat(str(sello["fecha"])).date()
            costo = Decimal(str(sello["costo"]))
        except (KeyError, ValueError, ArithmeticError):
            continue
        if fecha <= dia_alta and costo > 0:
            return costo
    return None


async def anclar_tenant(
    session: AsyncSession, tenant_id: uuid.UUID, *, apply: bool
) -> Informe:
    informe = Informe()
    # Como toda escritura del ledger: shared por tenant, así no corre a la par de
    # una deduplicación (que toma el exclusive).
    await maintenance_lock_service.acquire_write_lock_shared(session, tenant_id)
    historias = await cargar_historias(session, tenant_id)
    fuera = [pid for pid, h in historias.items() if h.stock_fuera_del_ledger]
    if not fuera:
        return informe
    m = InventoryMovement
    suma = dict(
        (
            await session.execute(
                select(m.product_id, func.coalesce(func.sum(m.qty), 0))
                .where(m.tenant_id == tenant_id, m.voided_at.is_(None), m.product_id.in_(fuera))
                .group_by(m.product_id)
            )
        )
        .tuples()
        .all()
    )
    productos = (
        (
            await session.execute(
                # Los inactivos (por ejemplo, duplicados ya fusionados) no se
                # anclan: su diferencia no es stock que el negocio tenga.
                select(Product).where(
                    Product.tenant_id == tenant_id,
                    Product.id.in_(fuera),
                    Product.is_active.is_(True),
                )
            )
        )
        .scalars()
        .all()
    )
    for p in productos:
        diferencia = int(p.stock_units or 0) - int(suma.get(p.id, 0))
        if diferencia < 0:
            informe.negativos += 1
            if len(informe.muestra_negativos) < MUESTRA:
                informe.muestra_negativos.append(
                    f"{p.name!r}: stock {p.stock_units}, ledger {suma.get(p.id, 0)}"
                )
            continue
        if diferencia == 0:
            continue
        alta = a_hora_de_negocio(p.created_at)
        ref = (await costos_de_referencia(session, tenant_id, [p.id], alta.date())).get(p.id)
        costo: Decimal | None = None
        if ref is not None and ref.origen in ("compra", "stock_inicial"):
            costo = ref.costo_unitario
        if costo is None:
            costo = _costo_tipeado_en_el_alta(p.custom_fields, alta.date())
        informe.anclados += 1
        informe.unidades += diferencia
        if costo is None:
            informe.sin_costo += 1
        if len(informe.muestra) < MUESTRA:
            informe.muestra.append(
                f"{p.name!r}: +{diferencia} el {alta.date()} a "
                f"{'sin costo' if costo is None else f'${costo}'}"
            )
        if not apply:
            continue
        contrato = contrato_declarado(costo)
        session.add(
            InventoryMovement(
                tenant_id=tenant_id,
                product_id=p.id,
                movement_type="adjustment",
                qty=diferencia,
                unit_cost=costo,
                reason="Stock cargado a mano antes de M·P2c (anclado por script)",
                source_event_id=f"manual:ancla:{p.id}",
                source_type=SOURCE_CATALOG_INITIAL_STOCK,
                occurred_at=momento_de_negocio_rotulado(p.created_at),
                **contrato.columnas(),
            )
        )
        # Sale de «fuera del ledger», una marca global: se rehace todo el
        # historial (la marca se escribe al confirmar, como cualquier otra).
        marcar_todo_el_historial(session, tenant_id, [p.id])
        await session.flush()
    return informe


async def _tenants(session: AsyncSession, args: argparse.Namespace) -> list[uuid.UUID]:
    if args.tenant_id:
        return [uuid.UUID(args.tenant_id)]
    if args.email:
        tid = (
            await session.execute(
                text("SELECT tenant_id FROM users WHERE lower(email) = lower(:e)"),
                {"e": args.email},
            )
        ).scalars().all()
        if len(tid) != 1:
            print(f"ERROR: el email corresponde a {len(tid)} tenants; usá --tenant-id.")
            sys.exit(2)
        return [tid[0]]
    return list((await session.execute(select(Product.tenant_id).distinct())).scalars())


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    quien = ap.add_mutually_exclusive_group(required=True)
    quien.add_argument("--tenant-id")
    quien.add_argument("--email")
    quien.add_argument("--all", action="store_true")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
    url, connect_args = async_engine_config()
    p = urlparse(url)
    print(f"Base: host={p.hostname} base={(p.path or '/')[1:]}")
    print("MODO:", "APPLY" if args.apply else "DRY-RUN (no escribe)")
    engine = crear_engine(url, connect_args=connect_args, echo=False)
    fabrica = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    total = Informe()
    try:
        async with fabrica() as session:
            tenants = await _tenants(session, args)
        for tenant_id in tenants:
            async with fabrica() as session:
                informe = await anclar_tenant(session, tenant_id, apply=args.apply)
                if args.apply:
                    await session.commit()
                else:
                    await session.rollback()
            if not (informe.anclados or informe.negativos):
                continue
            print(
                f"\n== tenant {tenant_id}: {informe.anclados} productos a anclar "
                f"({informe.unidades} unidades base), {informe.sin_costo} sin costo a la "
                f"fecha, {informe.negativos} con diferencia negativa (no se anclan)"
            )
            for linea in informe.muestra:
                print(f"   + {linea}")
            for linea in informe.muestra_negativos:
                print(f"   - {linea}")
            total.anclados += informe.anclados
            total.sin_costo += informe.sin_costo
            total.negativos += informe.negativos
            total.unidades += informe.unidades
    finally:
        await engine.dispose()
    print(
        f"\nTOTAL: {total.anclados} productos anclados ({total.unidades} unidades base), "
        f"{total.sin_costo} sin costo, {total.negativos} negativos."
        + ("" if args.apply else " DRY-RUN: no se escribió nada.")
    )


if __name__ == "__main__":
    asyncio.run(main())
