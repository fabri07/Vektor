"""Confirmar o corregir la fecha del stock inicial (M·2d).

Una apertura (``adjustment`` + ``catalog_initial_stock``) tiene fecha de negocio
confirmada sólo si la dio el usuario. Las que nacieron sin confirmar (pendientes,
o anteriores a M·2d) se confirman acá, por producto o por archivo, y una ya
confirmada se puede corregir. El usuario dice la fecha: nada se mueve solo.

Mover ``occurred_at`` es un UPDATE ORM del movimiento, así que lo ven los
listeners que ya existen: el del costo promedio (``costo_promedio_pendiente``
vigila ``occurred_at``) y el del costeo de ventas. El costo vigente del producto
se recalcula acá (``recalcular_costo_vigente``), porque la apertura puede pasar a
ser o dejar de ser la fuente más reciente.

**Las dos direcciones.** Adelantar la fecha hace del stock inicial una fuente
para las ventas anteriores a la carga: las que se registren desde ahora la usan;
las que ya quedaron ``sin_costo`` se recostean en M·2d-2 (el listener del costeo
sólo mira ventas nuevas o editadas). Atrasarla deja ventas YA costeadas con el
stock inicial y anteriores a la fecha nueva: su costo no se revalúa (M·2a), pero
se informan (cantidad e ids) en la respuesta y en ``decision_audit_log``.

**Locks:** siempre el producto antes que sus movimientos, en orden de id, en los
dos caminos: así dos confirmaciones (o una y un PATCH de costo, que recalcula el
mismo costo vigente) no se cruzan ni se pisan.

``tenant_id`` sale del JWT (lo pasa la ruta); cada consulta filtra por él.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Final

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services import maintenance_lock_service
from app.application.services.costo_vigente_service import recalcular_costo_vigente
from app.application.services.inventory_movement_origin import (
    FECHA_DE_USUARIO,
    SOURCE_CATALOG_INITIAL_STOCK,
    columnas_de_apertura_confirmada,
    momento_de_negocio_rotulado,
)
from app.persistence.models.audit import DecisionAuditLog
from app.persistence.models.inventory import InventoryMovement
from app.persistence.models.product import Product
from app.persistence.models.transaction import SaleEntry

DECISION_TYPE: Final = "OPENING_STOCK_CONFIRMED"
#: Cuántos ids de venta se devuelven y se auditan (la cantidad va siempre entera).
MAX_IDS: Final = 200


class SinAperturaError(Exception):
    """El producto (o el archivo) no tiene stock inicial vivo."""


class AperturaAmbiguaError(Exception):
    """El producto tiene más de un stock inicial cargado a mano: no se elige uno."""

    def __init__(self, cantidad: int) -> None:
        super().__init__(
            f"El producto tiene {cantidad} cargas a mano de stock inicial; "
            "no se puede elegir cuál corregir."
        )
        self.cantidad = cantidad


@dataclass
class ResultadoConfirmacion:
    aperturas: int = 0
    productos: int = 0
    #: Ventas costeadas con el stock inicial y anteriores a la fecha nueva.
    ventas_costeadas_antes: int = 0
    venta_ids: list[uuid.UUID] = field(default_factory=list)


def filtro_aperturas_vivas(tenant_id: uuid.UUID) -> list[ColumnElement[bool]]:
    m = InventoryMovement
    return [
        m.tenant_id == tenant_id,
        m.movement_type == "adjustment",
        m.source_type == SOURCE_CATALOG_INITIAL_STOCK,
        m.voided_at.is_(None),
    ]


async def confirmar_apertura_de_producto(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    product_id: uuid.UUID,
    fecha: datetime,
    user_id: uuid.UUID,
) -> ResultadoConfirmacion:
    """El stock inicial cargado A MANO del producto (sin archivo).

    El de un archivo se confirma desde el archivo (``confirmar_aperturas_de_archivo``):
    así un producto con una carga manual y otra importada nunca queda sin camino.
    """
    await maintenance_lock_service.acquire_write_lock_shared(session, tenant_id)
    await _bloquear_productos(session, tenant_id, [product_id])
    aperturas = list(
        (
            await session.execute(
                select(InventoryMovement)
                .where(
                    *filtro_aperturas_vivas(tenant_id),
                    InventoryMovement.product_id == product_id,
                    InventoryMovement.source_upload_id.is_(None),
                )
                .order_by(InventoryMovement.id)
                .with_for_update()
            )
        ).scalars()
    )
    if not aperturas:
        raise SinAperturaError(
            "El producto no tiene stock inicial cargado a mano; el de un archivo se "
            "confirma desde el archivo."
        )
    if len(aperturas) > 1:
        raise AperturaAmbiguaError(len(aperturas))
    return await _confirmar(session, tenant_id, aperturas, fecha, user_id, alcance="producto")


async def confirmar_aperturas_de_archivo(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    upload_id: uuid.UUID,
    fecha: datetime,
    user_id: uuid.UUID,
) -> ResultadoConfirmacion:
    await maintenance_lock_service.acquire_write_lock_shared(session, tenant_id)
    del_archivo = (
        *filtro_aperturas_vivas(tenant_id),
        InventoryMovement.source_upload_id == upload_id,
    )
    product_ids = list(
        (
            await session.execute(
                select(InventoryMovement.product_id).where(*del_archivo).distinct()
            )
        ).scalars()
    )
    await _bloquear_productos(session, tenant_id, product_ids)
    aperturas = list(
        (
            await session.execute(
                select(InventoryMovement)
                .where(*del_archivo)
                # Orden fijo: dos confirmaciones del mismo archivo no se cruzan.
                .order_by(InventoryMovement.product_id, InventoryMovement.id)
                .with_for_update()
            )
        ).scalars()
    )
    if not aperturas:
        raise SinAperturaError("El archivo no tiene stock inicial cargado.")
    return await _confirmar(session, tenant_id, aperturas, fecha, user_id, alcance="archivo")


async def _bloquear_productos(
    session: AsyncSession, tenant_id: uuid.UUID, product_ids: list[uuid.UUID]
) -> None:
    """``FOR UPDATE`` de los productos activos, en orden de id. Uno que no
    existe, es de otro tenant o está dado de baja → ``SinAperturaError``."""
    if not product_ids:
        return
    encontrados = list(
        (
            await session.execute(
                select(Product.id)
                .where(
                    Product.tenant_id == tenant_id,
                    Product.id.in_(product_ids),
                    Product.is_active.is_(True),
                )
                .order_by(Product.id)
                .with_for_update()
            )
        ).scalars()
    )
    if len(encontrados) != len(set(product_ids)):
        raise SinAperturaError("Producto no encontrado o dado de baja.")


async def _confirmar(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    aperturas: list[InventoryMovement],
    fecha: datetime,
    user_id: uuid.UUID,
    *,
    alcance: str,
) -> ResultadoConfirmacion:
    nueva = momento_de_negocio_rotulado(fecha)
    ahora = datetime.now(UTC)
    anteriores: dict[str, str] = {}
    for mov in aperturas:
        previa = mov.occurred_at or mov.created_at
        anteriores[str(mov.id)] = previa.isoformat() if previa is not None else ""
        mov.occurred_at = nueva
        for columna, valor in columnas_de_apertura_confirmada(
            FECHA_DE_USUARIO, user_id, ahora
        ).items():
            setattr(mov, columna, valor)
    product_ids = sorted({m.product_id for m in aperturas})
    await session.flush()
    await recalcular_costo_vigente(session, tenant_id, product_ids)

    total, ids = await _ventas_costeadas_antes(session, tenant_id, product_ids, nueva)
    resultado = ResultadoConfirmacion(
        aperturas=len(aperturas),
        productos=len(product_ids),
        ventas_costeadas_antes=total,
        venta_ids=ids,
    )
    session.add(
        DecisionAuditLog(
            tenant_id=tenant_id,
            decision_type=DECISION_TYPE,
            decision_data={
                "alcance": alcance,
                "fecha_nueva": nueva.isoformat(),
                "fechas_anteriores": anteriores,
                "productos": [str(p) for p in product_ids],
                "ventas_costeadas_antes": total,
                "venta_ids": [str(v) for v in ids],
            },
            triggered_by="ui:opening_confirmation",
            actor_user_id=user_id,
            created_at=ahora,
        )
    )
    await session.flush()
    return resultado


async def _ventas_costeadas_antes(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    product_ids: list[uuid.UUID],
    fecha: datetime,
) -> tuple[int, list[uuid.UUID]]:
    """Ventas vivas costeadas con el stock inicial y anteriores a ``fecha``.

    Su costo salió de una apertura que ahora resulta posterior. No se tocan.
    """
    condiciones = (
        SaleEntry.tenant_id == tenant_id,
        SaleEntry.product_id.in_(product_ids),
        SaleEntry.voided_at.is_(None),
        SaleEntry.cost_origin == "stock_inicial",
        func.date(SaleEntry.transaction_date) < fecha.date(),
    )
    total = (
        await session.execute(select(func.count()).select_from(SaleEntry).where(*condiciones))
    ).scalar_one()
    ids = list(
        (
            await session.execute(
                select(SaleEntry.id)
                .where(*condiciones)
                .order_by(SaleEntry.transaction_date, SaleEntry.id)
                .limit(MAX_IDS)
            )
        ).scalars()
    )
    return int(total), ids


__all__ = [
    "AperturaAmbiguaError",
    "ResultadoConfirmacion",
    "SinAperturaError",
    "confirmar_apertura_de_producto",
    "confirmar_aperturas_de_archivo",
    "filtro_aperturas_vivas",
]
