"""Lectura del costo de referencia por producto a una fecha (M·1a).

La regla vive en ``app/domain/costo_de_referencia.py``; acá sólo se traen las
candidatas. Por producto y por clase se trae UNA fila: la última viva con
costo > 0 y fecha de negocio ≤ corte, con el mismo orden que el dominio (fecha
de negocio → ``created_at`` → ``id``, descendente). El dominio decide la
precedencia entre las dos clases:

- **compra:** todo ``movement_type='purchase'`` vivo, cualquiera sea su
  ``source_type`` — incluida la mercadería que un catálogo declaró como COMPRA
  (``catalog_initial_stock`` + ``purchase``: genera COGS, es una compra real,
  igual que en ``PURCHASE_SOURCE_TYPES``);
- **stock inicial:** el saldo de APERTURA valorizado, que la ingesta guarda como
  ``adjustment`` + ``catalog_initial_stock`` (un activo que el negocio ya tenía,
  sin COGS ni caja).

El corte compara el timestamp contra el fin del día en UTC (no ``date()`` en
SQL, que depende del ``TimeZone`` de la sesión): así SQL y dominio, que mira
``fecha_negocio.date()`` del valor UTC, cortan igual.

``tenant_id`` lo pasa el llamador desde el JWT; un producto de otro tenant no
aparece en el resultado.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import ColumnElement, Select, and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.inventory_movement_origin import SOURCE_CATALOG_INITIAL_STOCK
from app.domain.costo_de_referencia import (
    CompraCandidata,
    CostoDeReferencia,
    costo_de_referencia,
)
from app.persistence.models.inventory import InventoryMovement, movement_business_time
from app.persistence.models.product import Product


def _aware(v: object) -> datetime:
    """SQLite devuelve el datetime sin zona o como texto; Postgres, aware."""
    dt = v if isinstance(v, datetime) else datetime.fromisoformat(str(v))
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _filtro_candidatas(
    tenant_id: uuid.UUID, ids: list[uuid.UUID], corte: date
) -> list[ColumnElement[bool]]:
    """Movimientos vivos con costo > 0 que pueden ser fuente a ``corte``."""
    m = InventoryMovement
    es_apertura = and_(
        m.movement_type == "adjustment", m.source_type == SOURCE_CATALOG_INITIAL_STOCK
    )
    fin_del_corte = datetime.combine(corte + timedelta(days=1), time.min, tzinfo=UTC)
    return [
        m.tenant_id == tenant_id,
        m.product_id.in_(ids),
        or_(m.movement_type == "purchase", es_apertura),
        m.voided_at.is_(None),
        func.coalesce(m.unit_cost_final, m.unit_cost) > 0,
        movement_business_time(m) < fin_del_corte,
    ]


def _columnas_candidata() -> list[Any]:
    m = InventoryMovement
    return [
        m.id,
        m.product_id,
        movement_business_time(m).label("fecha_negocio"),
        m.created_at,
        m.unit_cost,
        m.unit_cost_final,
        m.movement_type,
        m.shipping_treatment,
        m.tax_treatment,
        m.tax_rate,
        m.tax_amount,
        m.tax_recoverable,
        m.document_ref,
        m.cost_contract_version,
    ]


def consulta_todas_las_candidatas(
    tenant_id: uuid.UUID, product_ids: Iterable[uuid.UUID], corte: date
) -> Select[Any]:
    """TODAS las candidatas hasta ``corte`` (no sólo la última): para elegir la
    fuente de varias fechas con una sola consulta (costeo de ventas, M·2a)."""
    ids = list(dict.fromkeys(product_ids))
    return select(*_columnas_candidata()).where(*_filtro_candidatas(tenant_id, ids, corte))


def candidata_de_fila(f: Any) -> CompraCandidata:
    return CompraCandidata(
        movement_id=f.id,
        fecha_negocio=_aware(f.fecha_negocio),
        cargada_en=_aware(f.created_at),
        costo_facturado=f.unit_cost,
        costo_final=f.unit_cost_final,
        es_stock_inicial=f.movement_type == "adjustment",
        shipping_treatment=f.shipping_treatment,
        tax_treatment=f.tax_treatment,
        tax_rate=f.tax_rate,
        tax_amount=f.tax_amount,
        tax_recoverable=f.tax_recoverable,
        document_ref=f.document_ref,
        contract_version=f.cost_contract_version,
    )


async def costos_de_referencia(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    product_ids: Iterable[uuid.UUID],
    corte: date,
) -> dict[uuid.UUID, CostoDeReferencia]:
    """Costo de referencia a ``corte`` de cada producto del tenant pedido."""
    ids = list(dict.fromkeys(product_ids))
    if not ids:
        return {}

    m = InventoryMovement
    negocio = movement_business_time(m)
    ranked = (
        select(
            *_columnas_candidata(),
            func.row_number()
            .over(
                partition_by=[m.product_id, m.movement_type],
                order_by=[negocio.desc(), m.created_at.desc(), m.id.desc()],
            )
            .label("rn"),
        )
        .where(*_filtro_candidatas(tenant_id, ids, corte))
        .subquery()
    )
    filas = (await session.execute(select(ranked).where(ranked.c.rn == 1))).all()

    candidatas: dict[uuid.UUID, list[CompraCandidata]] = {}
    for f in filas:
        candidatas.setdefault(f.product_id, []).append(candidata_de_fila(f))

    cargados: dict[uuid.UUID, Decimal | None] = dict(
        (
            await session.execute(
                select(Product.id, Product.unit_cost_ars).where(
                    Product.tenant_id == tenant_id, Product.id.in_(ids)
                )
            )
        )
        .tuples()
        .all()
    )
    return {
        pid: costo_de_referencia(candidatas.get(pid, []), corte, cargado)
        for pid, cargado in cargados.items()
    }
