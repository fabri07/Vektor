"""Recalcula ``products.unit_cost_ars`` desde sus fuentes fechadas (M·1c).

La regla es ``domain/costo_vigente.py``; acá sólo se juntan las fuentes: la
compra de referencia a HOY (``costo_de_referencia_repository``, M·1a) y la
edición manual y de catálogo guardadas en el producto. Recalculan los eventos
que pueden cambiar el costo vigente con una fecha que no es «ahora»: registrar
una compra (puede ser atrasada) y anular o reactivar una (puede ser la que había
ganado). Una edición manual o de catálogo es «ahora» y la escribe su propia ruta
con ``registrar_edicion`` / ``aplicar_costo_de_catalogo``.

**Dos formas de usarlo:**

- ``recalcular_costo_vigente``: inmediato (compra suelta por ``increment_stock``).
- ``marcar_para_recalcular`` + ``recalcular_pendientes`` (M·1c-2): los caminos en
  lote (ingesta, remito, anulaciones de borrado y relectura) MARCAN el producto
  en ``session.info`` y el dueño del lote recalcula UNA vez al final. Fila por
  fila sería una consulta por compra y, con ``autoflush=False``, leería un
  estado a medio armar.

``tenant_id`` lo pasa el llamador desde el JWT; los productos de otro tenant no
se tocan. Hace ``flush`` antes de leer: los movimientos recién creados tienen que
ser candidatos.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection, Iterable

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

#: ``session.info[_PENDIENTES]`` = ``{tenant_id: {product_id: {movimientos nuevos}}}``.
_PENDIENTES = "m1c_costos_pendientes"


async def recalcular_costo_vigente(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    product_ids: Iterable[uuid.UUID],
    *,
    movimientos_nuevos: Collection[uuid.UUID] = (),
) -> set[uuid.UUID]:
    """Recalcula y devuelve los productos cuyo costo o procedencia se escribió.

    ``movimientos_nuevos``: las compras que se acaban de registrar. Un producto
    sin procedencia (costo anterior a M·1c) sólo cambia si una de ESAS compras es
    la más reciente (ver ``decidir_costo_vigente``).
    """
    ids = list(dict.fromkeys(product_ids))
    if not ids:
        return set()
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
    escritos: set[uuid.UUID] = set()
    for producto in productos:
        referencia = referencias.get(producto.id)
        if referencia is None:
            continue
        vigente = decidir_costo_vigente(
            referencia,
            leer_costo_fechado(producto.custom_fields, MANUAL_FIELD),
            leer_costo_fechado(producto.custom_fields, CATALOGO_FIELD),
            # Sin procedencia Y con costo: un costo vacío o 0 no se protege
            # (antes de M·1c una compra lo llenaba igual).
            sin_procedencia=(
                ORIGEN_FIELD not in (producto.custom_fields or {})
                and producto.unit_cost_ars is not None
                and producto.unit_cost_ars > 0
            ),
            movimientos_nuevos=movimientos_nuevos,
        )
        if vigente is None:
            continue
        producto.unit_cost_ars = vigente.costo
        producto.custom_fields = sellar(producto.custom_fields, vigente)
        escritos.add(producto.id)
    # Escrito ya: un `refresh` posterior del llamador no puede descartarlo.
    await session.flush()
    return escritos


def marcar_para_recalcular(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    product_id: uuid.UUID,
    movimiento_nuevo: uuid.UUID | None = None,
) -> None:
    """Anota el producto para el recálculo del final del lote (M·1c-2)."""
    por_tenant = session.info.setdefault(_PENDIENTES, {})
    nuevos = por_tenant.setdefault(tenant_id, {}).setdefault(product_id, set())
    if movimiento_nuevo is not None:
        nuevos.add(movimiento_nuevo)


async def recalcular_pendientes(session: AsyncSession, tenant_id: uuid.UUID) -> set[uuid.UUID]:
    """Recalcula lo marcado para ESTE tenant y lo saca de la cola.

    Devuelve los productos marcados (escritos o no): el llamador que guarda un
    ledger completa su ``after`` con el costo que quedó.
    """
    por_tenant = session.info.get(_PENDIENTES) or {}
    marcados: dict[uuid.UUID, set[uuid.UUID]] = por_tenant.pop(tenant_id, {})
    if not marcados:
        return set()
    nuevos = set().union(*marcados.values())
    await recalcular_costo_vigente(session, tenant_id, marcados, movimientos_nuevos=nuevos)
    return set(marcados)
