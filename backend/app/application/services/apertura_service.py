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

**La fecha de la fila (M·2d-1b).** Un import fecha la apertura con la fecha
propia de la fila (``acquired_at``) y la deja con ``opening_date_source='archivo'``
SIN confirmar; sin fecha propia, queda pendiente en la hora de carga. Confirmar
por archivo con ``aceptar_fechas_de_filas`` confirma las de la fila con SU fecha
(no se mueven: ya costearon sus ventas al importarse) y le da ``fecha`` al resto;
sin aceptarlas, ``fecha`` pisa todas.

**Locks:** siempre el producto antes que sus movimientos, en orden de id, en los
dos caminos: así dos confirmaciones (o una y un PATCH de costo, que recalcula el
mismo costo vigente) no se cruzan ni se pisan.

``tenant_id`` sale del JWT (lo pasa la ruta); cada consulta filtra por él.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Final

from sqlalchemy import ColumnElement, and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services import maintenance_lock_service
from app.application.services.costo_vigente_service import recalcular_costo_vigente
from app.application.services.inventory_movement_origin import (
    FECHA_DE_ARCHIVO,
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


class FechaRequeridaError(Exception):
    """Hay stock inicial sin fecha propia y no se dio una fecha."""


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


@dataclass(frozen=True)
class ResumenAperturasDeArchivo:
    """El stock inicial sin confirmar de UN archivo, para pedir la fecha al
    terminar de importarlo (M·2d-1b). Lo que se cargó de verdad, leído de la base."""

    aperturas: int
    productos: int
    #: En unidades de VENTA (``qty`` del ledger está en unidades base).
    unidades: Decimal
    #: Sin fecha propia: figuran desde la hora de carga.
    pendientes: int
    #: Con la fecha de su fila (``opening_date_source='archivo'``), sin aceptar.
    con_fecha_de_fila: int
    fecha_de_fila_min: date | None
    fecha_de_fila_max: date | None
    #: El momento cero (primer día de ventas o gastos): una SUGERENCIA, nunca fecha.
    fecha_sugerida: date | None


def filtro_aperturas_vivas(tenant_id: uuid.UUID) -> list[ColumnElement[bool]]:
    """Stock inicial vivo de productos ACTIVOS: los INGRESOS.

    Un catálogo con menos stock que el actual escribe un ajuste
    ``catalog_initial_stock`` negativo: es una baja, no stock inicial, y no tiene
    "desde cuándo" que confirmar. Un producto dado de baja no anula sus
    movimientos: sin este filtro, su apertura seguiría en el aviso y bloquearía
    la confirmación de todo su archivo.
    """
    m = InventoryMovement
    return [
        m.tenant_id == tenant_id,
        m.movement_type == "adjustment",
        m.source_type == SOURCE_CATALOG_INITIAL_STOCK,
        m.voided_at.is_(None),
        m.qty > 0,
        m.product_id.in_(
            select(Product.id).where(
                Product.tenant_id == tenant_id, Product.is_active.is_(True)
            )
        ),
    ]


async def apertura_viva_de(
    session: AsyncSession, tenant_id: uuid.UUID, product_id: uuid.UUID
) -> InventoryMovement | None:
    """La apertura viva más vieja del producto, manual o de archivo (B·5c)."""
    return (
        await session.execute(
            select(InventoryMovement)
            .where(*filtro_aperturas_vivas(tenant_id), InventoryMovement.product_id == product_id)
            .order_by(InventoryMovement.created_at, InventoryMovement.id)
            .limit(1)
        )
    ).scalar_one_or_none()


async def productos_con_apertura_viva(
    session: AsyncSession, tenant_id: uuid.UUID, product_ids: list[uuid.UUID]
) -> set[uuid.UUID]:
    """DEFINICIÓN ÚNICA (B·5c): un producto activo «necesita stock inicial» si
    NO está en este conjunto. La usan la apertura a mano, el lote, la respuesta
    del producto y la pantalla. Tenga ventas o no."""
    if not product_ids:
        return set()
    filas = await session.execute(
        select(InventoryMovement.product_id)
        .where(
            *filtro_aperturas_vivas(tenant_id),
            InventoryMovement.product_id.in_(product_ids),
        )
        .distinct()
    )
    return {pid for pid in filas.scalars() if pid is not None}


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
    return await _confirmar(
        session, tenant_id, [(m, fecha) for m in aperturas], user_id, alcance="producto"
    )


async def confirmar_aperturas_de_archivo(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    upload_id: uuid.UUID,
    fecha: datetime | None,
    user_id: uuid.UUID,
    *,
    aceptar_fechas_de_filas: bool = False,
) -> ResultadoConfirmacion:
    """Todas las aperturas vivas del archivo, en una transacción.

    Con ``aceptar_fechas_de_filas`` las que traen fecha de su fila se confirman
    con esa fecha y el resto toma ``fecha``; sin ``fecha`` y con alguna sin fecha
    propia → ``FechaRequeridaError``. Sin aceptarlas, ``fecha`` (obligatoria) es
    la de todas.
    """
    from app.application.services.facts_provider import _dia_de_negocio  # noqa: PLC0415
    from app.domain.business_time import today_ar  # noqa: PLC0415

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

    def conserva(m: InventoryMovement) -> bool:
        # Defensa: una fecha de fila futura nunca se confirma (la ingesta no la
        # persiste; si llegara, se trata como sin fecha y se pide una).
        dia = _dia_de_negocio(m.occurred_at)
        return (
            aceptar_fechas_de_filas
            and m.opening_date_source == FECHA_DE_ARCHIVO
            and dia is not None
            and dia <= today_ar()
        )

    # ``None`` = confirmar con la fecha que ya tiene (la de su fila).
    plan = [(m, None if conserva(m) else fecha) for m in aperturas]
    if any(f is None and not conserva(m) for m, f in plan):
        raise FechaRequeridaError(
            "Indicá desde qué fecha tenías el stock que no trae fecha en el archivo."
            if aceptar_fechas_de_filas
            else "Indicá desde qué fecha tenías este stock."
        )
    return await _confirmar(session, tenant_id, plan, user_id, alcance="archivo")


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
    plan: list[tuple[InventoryMovement, datetime | None]],
    user_id: uuid.UUID,
    *,
    alcance: str,
) -> ResultadoConfirmacion:
    """Confirma cada apertura con su fecha: la dada (``usuario``) o, con
    ``None``, la que ya tiene de su fila (``archivo``)."""
    ahora = datetime.now(UTC)
    anteriores: dict[str, str] = {}
    de_archivo: list[str] = []
    #: La fecha que quedó, por producto (la más temprana si hay varias).
    por_producto: dict[uuid.UUID, datetime] = {}
    nuevas: set[str] = set()
    for mov, fecha in plan:
        previa = mov.occurred_at or mov.created_at
        anteriores[str(mov.id)] = previa.isoformat() if previa is not None else ""
        if fecha is None:
            fuente = FECHA_DE_ARCHIVO
            de_archivo.append(str(mov.id))
        else:
            fuente = FECHA_DE_USUARIO
            mov.occurred_at = momento_de_negocio_rotulado(fecha)
        for columna, valor in columnas_de_apertura_confirmada(fuente, user_id, ahora).items():
            setattr(mov, columna, valor)
        queda = mov.occurred_at
        assert queda is not None  # la de la fila o la recién puesta
        if queda.tzinfo is None:  # SQLite la devuelve sin zona
            queda = queda.replace(tzinfo=UTC)
        nuevas.add(queda.isoformat())
        if mov.product_id not in por_producto or queda < por_producto[mov.product_id]:
            por_producto[mov.product_id] = queda
    product_ids = sorted(por_producto)
    await session.flush()
    await recalcular_costo_vigente(session, tenant_id, product_ids)

    total, ids = await _ventas_costeadas_antes(session, tenant_id, por_producto)
    resultado = ResultadoConfirmacion(
        aperturas=len(plan),
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
                "fechas_nuevas": sorted(nuevas),
                "fechas_anteriores": anteriores,
                "con_fecha_de_su_fila": de_archivo,
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
    por_producto: dict[uuid.UUID, datetime],
) -> tuple[int, list[uuid.UUID]]:
    """Ventas vivas costeadas con el stock inicial y anteriores a la fecha que
    quedó en su producto.

    Su costo salió de una apertura que ahora resulta posterior. No se tocan.
    Una condición por día (suele ser uno solo por confirmación).
    """
    por_dia: dict[date, list[uuid.UUID]] = {}
    for pid, fecha in por_producto.items():
        por_dia.setdefault(fecha.date(), []).append(pid)
    condiciones = (
        SaleEntry.tenant_id == tenant_id,
        SaleEntry.voided_at.is_(None),
        SaleEntry.cost_origin == "stock_inicial",
        or_(
            *(
                and_(
                    SaleEntry.product_id.in_(pids),
                    func.date(SaleEntry.transaction_date) < dia,
                )
                for dia, pids in por_dia.items()
            )
        ),
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


async def resumen_de_archivo(
    session: AsyncSession, tenant_id: uuid.UUID, upload_id: uuid.UUID
) -> ResumenAperturasDeArchivo | None:
    """El stock inicial sin confirmar del archivo, para pedir la fecha al terminar
    de importarlo. SELECT-only; ``None`` si no hay nada que confirmar."""
    from app.application.services.facts_provider import _dia_de_negocio  # noqa: PLC0415
    from app.application.services.ingestion_import_service import (  # noqa: PLC0415
        _opening_stock_date,
    )

    m = InventoryMovement
    filas = (
        await session.execute(
            select(m.product_id, m.qty, m.opening_date_source, m.occurred_at,
                   Product.base_units_per_sale_unit)
            .join(Product, (Product.id == m.product_id) & (Product.tenant_id == tenant_id))
            .where(
                *filtro_aperturas_vivas(tenant_id),
                m.source_upload_id == upload_id,
                m.opening_confirmed_at.is_(None),
            )
        )
    ).all()
    if not filas:
        return None
    unidades = sum(
        (Decimal(qty) / Decimal(factor or 1) for _, qty, _, _, factor in filas), Decimal(0)
    )
    dias_de_fila = [
        d
        for _, _, fuente, ocurrio, _ in filas
        if fuente == FECHA_DE_ARCHIVO and (d := _dia_de_negocio(ocurrio)) is not None
    ]
    sugerida = await _opening_stock_date(session, tenant_id, [])
    return ResumenAperturasDeArchivo(
        aperturas=len(filas),
        productos=len({f[0] for f in filas}),
        unidades=unidades.quantize(Decimal("0.001")),
        pendientes=len(filas) - len(dias_de_fila),
        con_fecha_de_fila=len(dias_de_fila),
        fecha_de_fila_min=min(dias_de_fila, default=None),
        fecha_de_fila_max=max(dias_de_fila, default=None),
        fecha_sugerida=sugerida.date() if sugerida is not None else None,
    )


__all__ = [
    "AperturaAmbiguaError",
    "apertura_viva_de",
    "productos_con_apertura_viva",
    "FechaRequeridaError",
    "ResumenAperturasDeArchivo",
    "ResultadoConfirmacion",
    "SinAperturaError",
    "confirmar_apertura_de_producto",
    "confirmar_aperturas_de_archivo",
    "filtro_aperturas_vivas",
    "resumen_de_archivo",
]
