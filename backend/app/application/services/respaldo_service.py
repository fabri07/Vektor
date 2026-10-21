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
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Final

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.stock_service import SaleProductNotFoundError
from app.persistence.models.inventory import InventoryMovement, movement_business_time
from app.persistence.models.product import Product
from app.persistence.models.transaction import SaleEntry
from app.persistence.repositories.costo_de_referencia_repository import filtro_de_fuentes

#: Sin corte práctico: el primer día se compara después contra cada venta.
_SIN_CORTE: Final = date(9999, 12, 30)

MOTIVO_VENTA_SIN_COMPRA: Final = "Venta sin compra previa"
MOTIVO_VENTA_SIN_PRODUCTO: Final = "Venta sin producto"
#: B·5e-2: la venta del POS sin red ya cobrada entra; la tarea queda en «Otros».
MOTIVO_VENTA_POS_SIN_COMPRA: Final = "Venta del POS sin compra previa"


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


async def productos_con_fuente_en_archivo(
    session: AsyncSession, tenant_id: uuid.UUID, uploaded_file_id: uuid.UUID
) -> set[uuid.UUID]:
    """Productos con una fuente con costo viva que trae ESTE archivo."""
    m = InventoryMovement
    # El mismo filtro de fuentes que el costeo (sin su `product_id IN`, que es el
    # término [1]), acotado a este archivo.
    fuentes = filtro_de_fuentes(tenant_id, [], _SIN_CORTE)
    filas = await session.execute(
        select(m.product_id)
        .where(*fuentes[:1], *fuentes[2:], m.source_upload_id == uploaded_file_id, m.qty > 0)
        .distinct()
    )
    return {pid for pid in filas.scalars() if pid is not None}


@dataclass(frozen=True)
class VentasSinRespaldo:
    """Ventas vivas que perderían su compra previa (B·5e-2)."""

    total: int
    muestras: list[dict[str, str]]


async def ventas_que_pierden_respaldo(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    antes: dict[uuid.UUID, date],
    product_ids: Iterable[uuid.UUID],
    *,
    excluir_upload_id: uuid.UUID | None = None,
    muestras: int = 5,
) -> VentasSinRespaldo:
    """Con las anulaciones ya escritas (flush), las ventas vivas de esos
    productos que ANTES estaban respaldadas y AHORA no: su día cae entre el
    primer día de fuente de antes y el de ahora (o ya no hay fuente). Una venta
    que ya no tenía respaldo (anterior a B·5e) no cuenta: no la rompe esto."""
    ids = sorted({p for p in product_ids if p is not None and p in antes}, key=str)
    if not ids:
        return VentasSinRespaldo(0, [])
    despues = await primeros_dias_de_fuente(session, tenant_id, ids)
    afectados = [p for p in ids if despues.get(p) is None or despues[p] > antes[p]]
    if not afectados:
        return VentasSinRespaldo(0, [])
    filas = (
        await session.execute(
            select(SaleEntry.id, SaleEntry.product_id, SaleEntry.transaction_date, Product.name)
            .join(Product, Product.id == SaleEntry.product_id)
            .where(
                SaleEntry.tenant_id == tenant_id,
                SaleEntry.voided_at.is_(None),
                SaleEntry.product_id.in_(afectados),
                # Sólo las que podían estar respaldadas: desde el primer día viejo.
                SaleEntry.transaction_date
                >= datetime.combine(min(antes[p] for p in afectados), datetime.min.time()),
                # Las del propio archivo no son «de otro lado» (la relectura las
                # juzga con su propia regla).
                *(
                    [
                        or_(
                            SaleEntry.source_upload_id.is_(None),
                            SaleEntry.source_upload_id != excluir_upload_id,
                        )
                    ]
                    if excluir_upload_id is not None
                    else []
                ),
            )
            .order_by(SaleEntry.transaction_date)
        )
    ).all()
    perdidas = []
    for venta_id, pid, cuando, nombre in filas:
        dia = cuando.date()
        if dia >= antes[pid] and not respaldada(despues.get(pid), dia):
            perdidas.append(
                {"sale_id": str(venta_id), "producto": nombre, "fecha": dia.isoformat()}
            )
    return VentasSinRespaldo(len(perdidas), perdidas[:muestras])


__all__ = [
    "MOTIVO_VENTA_POS_SIN_COMPRA",
    "MOTIVO_VENTA_SIN_COMPRA",
    "MOTIVO_VENTA_SIN_PRODUCTO",
    "VentaSinCompraError",
    "VentasSinRespaldo",
    "productos_con_fuente_en_archivo",
    "ventas_que_pierden_respaldo",
    "VentaSinProductoError",
    "exigir_respaldo",
    "primeros_dias_de_fuente",
    "respaldada",
]
