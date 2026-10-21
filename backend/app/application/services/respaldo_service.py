"""Toda venta con compra previa (B·5e): el respaldo de una venta.

Decisión del dueño (2026-10-20/21): «toda venta conlleva una compra previa» y
«tiene que haber un control real del stock, no se puede vender algo que no se
compró». Una venta del producto P el día D está **respaldada** si:

1. **Hay compra previa:** existe una fuente con costo de P —una compra viva o un
   stock inicial vivo, con costo > 0— de fecha de negocio ≤ D. Es el MISMO
   filtro que usa el costeo (``costo_de_referencia_repository.filtro_de_fuentes``)
   más ``qty > 0`` (una baja de catálogo no es una entrada). Equivale a
   ``primer día de fuente de P ≤ D``.
2. **Hay cantidad:** el stock no queda negativo. Esa regla ya rige en cada camino
   en vivo (``check_stock_available``, ``InsufficientStockError``, los
   ``FOR UPDATE`` de la venta manual y del POS); este módulo no la duplica.

El respaldo NO es la fuente del costo: el costo lo sigue eligiendo el listener
(compra, stock inicial, edición manual, catálogo), con la política aprobada. Por
eso no hay columna que lo guarde: se calcula, y al borrar un archivo o anular una
compra basta con recalcular el primer día de fuente de los productos tocados.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from datetime import UTC, date, datetime
from typing import Final

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.stock_service import SaleProductNotFoundError
from app.persistence.models.inventory import InventoryMovement, movement_business_time
from app.persistence.models.product import Product
from app.persistence.repositories.costo_de_referencia_repository import filtro_de_fuentes

#: Sin corte práctico: el primer día se compara después contra cada venta.
_SIN_CORTE: Final = date(9999, 12, 30)

MOTIVO_VENTA_SIN_COMPRA: Final = "Venta sin compra previa"
MOTIVO_VENTA_SIN_PRODUCTO: Final = "Venta sin producto"


def _dia(valor: object) -> date:
    """Día de negocio de ``movement_business_time``: hora argentina ROTULADA como
    UTC (Postgres la devuelve aware; SQLite, sin zona o como texto)."""
    dt = valor if isinstance(valor, datetime) else datetime.fromisoformat(str(valor))
    if dt.tzinfo is not None:
        dt = dt.astimezone(UTC)
    return dt.date()


async def primeros_dias_de_fuente(
    session: AsyncSession, tenant_id: uuid.UUID, product_ids: Iterable[uuid.UUID]
) -> dict[uuid.UUID, date]:
    """``{producto: primer día con una fuente con costo}``; sin fuente → ausente."""
    ids = sorted({pid for pid in product_ids if pid is not None})
    if not ids:
        return {}
    m = InventoryMovement
    filas = await session.execute(
        select(m.product_id, func.min(movement_business_time(m)))
        .where(*filtro_de_fuentes(tenant_id, ids, _SIN_CORTE), m.qty > 0)
        .group_by(m.product_id)
    )
    return {pid: _dia(primero) for pid, primero in filas.all() if pid is not None and primero}


def respaldada(primer_dia: date | None, dia_venta: date) -> bool:
    return primer_dia is not None and primer_dia <= dia_venta


class VentaSinCompraError(Exception):
    """Una venta de un producto sin compra ni stock inicial con costo a su fecha."""

    def __init__(self, product_id: uuid.UUID, nombre: str, dia: date) -> None:
        super().__init__(product_id, nombre, dia)
        self.product_id = product_id
        self.nombre = nombre
        self.dia = dia

    @property
    def user_message(self) -> str:
        # El chat muestra ``user_message`` tal cual (``agent.py``).
        return (
            f"No hay compra ni stock inicial de «{self.nombre}» al {self.dia:%d/%m/%Y}: "
            "no se puede vender algo que no se compró. Cargá la compra o el stock "
            "inicial y volvé a registrar la venta."
        )

    def __str__(self) -> str:
        return self.user_message


class VentaSinProductoError(Exception):
    """Una venta a mano sin producto (decisión del dueño, 2026-10-21)."""

    user_message = (
        "Elegí qué producto vendiste: una venta sin producto no tiene compra previa "
        "ni costo."
    )

    def __str__(self) -> str:
        return self.user_message


async def exigir_respaldo(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    ventas: Iterable[tuple[uuid.UUID | None, date]],
) -> None:
    """Levanta la primera ``VentaSinProductoError`` / ``VentaSinCompraError``.

    ``ventas``: ``(product_id, día de negocio)``. La cantidad la controla cada
    camino con su chequeo de stock (ver el docstring del módulo).
    """
    lista = list(ventas)
    if any(pid is None for pid, _ in lista):
        raise VentaSinProductoError()
    ids: set[uuid.UUID] = {p for p, _ in lista if p is not None}
    filas = await session.execute(
        select(Product.id, Product.name).where(
            Product.tenant_id == tenant_id, Product.id.in_(ids)
        )
    )
    nombres: dict[uuid.UUID, str] = {fila[0]: fila[1] for fila in filas.all()}
    for pid in sorted(ids - set(nombres), key=str):
        # Inexistente o de OTRO negocio: el mismo 400 de siempre, sin decir si
        # ese id tiene compras en ningún lado.
        raise SaleProductNotFoundError(pid)
    primeros = await primeros_dias_de_fuente(session, tenant_id, ids)
    for producto, dia in lista:
        assert producto is not None
        if not respaldada(primeros.get(producto), dia):
            raise VentaSinCompraError(producto, nombres[producto], dia)


__all__ = [
    "MOTIVO_VENTA_SIN_COMPRA",
    "MOTIVO_VENTA_SIN_PRODUCTO",
    "VentaSinCompraError",
    "VentaSinProductoError",
    "exigir_respaldo",
    "primeros_dias_de_fuente",
    "respaldada",
]
