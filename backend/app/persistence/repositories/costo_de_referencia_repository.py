"""Lectura del costo de referencia por producto a una fecha (M·1a).

La regla vive en ``app/domain/costo_de_referencia.py``; acá sólo se traen las
candidatas. Por producto y por clase (compra real / stock inicial) se trae UNA
fila: la última compra viva con costo > 0 y fecha de negocio ≤ corte, con el
mismo orden que el dominio (fecha de negocio → ``created_at`` → ``id``,
descendente). El dominio decide la precedencia entre las dos.

``tenant_id`` lo pasa el llamador desde el JWT; un producto de otro tenant no
aparece en el resultado.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import func, select
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
    es_inicial = func.coalesce(m.source_type == SOURCE_CATALOG_INITIAL_STOCK, False)
    costo = func.coalesce(m.unit_cost_final, m.unit_cost)
    ranked = (
        select(
            m.id,
            m.product_id,
            negocio.label("fecha_negocio"),
            m.created_at,
            m.unit_cost,
            m.unit_cost_final,
            m.source_type,
            m.shipping_treatment,
            m.tax_treatment,
            m.tax_rate,
            m.tax_amount,
            m.tax_recoverable,
            m.document_ref,
            m.cost_contract_version,
            func.row_number()
            .over(
                partition_by=[m.product_id, es_inicial],
                order_by=[negocio.desc(), m.created_at.desc(), m.id.desc()],
            )
            .label("rn"),
        )
        .where(
            m.tenant_id == tenant_id,
            m.product_id.in_(ids),
            m.movement_type == "purchase",
            m.voided_at.is_(None),
            costo > 0,
            func.date(negocio) <= corte,
        )
        .subquery()
    )
    filas = (await session.execute(select(ranked).where(ranked.c.rn == 1))).all()

    candidatas: dict[uuid.UUID, list[CompraCandidata]] = {}
    for f in filas:
        candidatas.setdefault(f.product_id, []).append(
            CompraCandidata(
                movement_id=f.id,
                fecha_negocio=_aware(f.fecha_negocio),
                cargada_en=_aware(f.created_at),
                costo_facturado=f.unit_cost,
                costo_final=f.unit_cost_final,
                es_stock_inicial=f.source_type == SOURCE_CATALOG_INITIAL_STOCK,
                shipping_treatment=f.shipping_treatment,
                tax_treatment=f.tax_treatment,
                tax_rate=f.tax_rate,
                tax_amount=f.tax_amount,
                tax_recoverable=f.tax_recoverable,
                document_ref=f.document_ref,
                contract_version=f.cost_contract_version,
            )
        )

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
