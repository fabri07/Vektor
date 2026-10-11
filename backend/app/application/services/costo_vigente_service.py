"""Recalcula ``products.unit_cost_ars`` desde sus fuentes fechadas (M·1c).

La regla es ``domain/costo_vigente.py``; acá sólo se juntan las fuentes: la
compra de referencia a HOY (``costo_de_referencia_repository``, M·1a) y la
edición manual y de catálogo guardadas en el producto. Se llama después de
registrar o anular una compra, que son los únicos eventos que pueden cambiar el
costo vigente con una fecha que no es «ahora» (una compra atrasada, o la
anulación de la que había ganado). Una edición manual o de catálogo es «ahora» y
la escribe su propia ruta con ``registrar_edicion``.

``tenant_id`` lo pasa el llamador desde el JWT; los productos de otro tenant no
se tocan. Hace ``flush`` antes de leer: el movimiento recién creado tiene que
ser candidato.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.business_time import today_ar
from app.domain.costo_vigente import (
    CATALOGO_FIELD,
    MANUAL_FIELD,
    ORIGEN_FIELD,
    decidir_costo_vigente,
    leer_costo_fechado,
    sellar,
)
from app.persistence.models.product import Product
from app.persistence.repositories.costo_de_referencia_repository import costos_de_referencia


async def recalcular_costo_vigente(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    product_ids: Iterable[uuid.UUID],
    *,
    movimiento_nuevo: uuid.UUID | None = None,
) -> None:
    """``movimiento_nuevo``: la compra que se acaba de registrar. Un producto sin
    procedencia (costo anterior a M·1c) sólo cambia si ESA compra es la más
    reciente (ver ``decidir_costo_vigente``)."""
    ids = list(dict.fromkeys(product_ids))
    if not ids:
        return
    await session.flush()
    referencias = await costos_de_referencia(session, tenant_id, ids, today_ar())
    productos = (
        (
            await session.execute(
                select(Product).where(Product.tenant_id == tenant_id, Product.id.in_(ids))
            )
        )
        .scalars()
        .all()
    )
    for producto in productos:
        referencia = referencias.get(producto.id)
        if referencia is None:
            continue
        vigente = decidir_costo_vigente(
            referencia,
            leer_costo_fechado(producto.custom_fields, MANUAL_FIELD),
            leer_costo_fechado(producto.custom_fields, CATALOGO_FIELD),
            sin_procedencia=ORIGEN_FIELD not in (producto.custom_fields or {}),
            movimiento_nuevo=movimiento_nuevo,
        )
        if vigente is None:
            continue
        producto.unit_cost_ars = vigente.costo
        producto.custom_fields = sellar(producto.custom_fields, vigente)
    # Escrito ya: un `refresh` posterior del llamador no puede descartarlo.
    await session.flush()
