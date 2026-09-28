"""Deudas de stock de la caja: registrarlas, mostrarlas y resolverlas (B11).

Una venta cobrada sin conexión puede llegar cuando el stock ya no alcanza.
Rechazarla no deshace nada —la plata entró, la mercadería salió—, así que la
venta entra completa y lo que queda es una **salida pendiente** que un humano
resuelve. Véktor no la corrige solo: no sabe si la mercadería estaba o si la
venta está mal, y adivinar deja el stock peor.

**Por qué DOS resoluciones y no las tres del plan maestro.** El plan proponía
además ``register_shrinkage`` ("el faltante era merma: descontá lo que hay y
registrá la pérdida"). Esa no cierra. El chequeo agregado calcula
``esperado = ancla + entradas − ventas vivas + mermas``: con ancla 3 y una venta
de 5, descontar 3 y registrar 2 de merma da ``3 − 5 − 2 = −4`` contra un stock
real de 0. La merma explica que el sistema tenga **más** de lo que hay; acá
tenía **menos** de lo que se vendió. Las dos que sí cierran son:

- ``ajuste_de_entrada`` — la mercadería estaba: entra el faltante como ajuste de
  reconciliación y después sale la venta completa.
- ``anular_ticket`` — la venta está mal: se anula el ticket entero (B10).

**El faltante se recalcula AL RESOLVER**, nunca se usa el de la captura: entre
la venta y la conciliación pudo haber compras y ventas, y con el número viejo el
ajuste no cuadraría.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services import stock_service
from app.persistence.models.audit import DecisionAuditLog
from app.persistence.models.pos_operation import PosOperation
from app.persistence.models.pos_stock_discrepancy import (
    ESTADO_ABIERTA,
    ESTADO_RESUELTA,
    RESOLUCION_AJUSTE,
    PosStockDiscrepancy,
)
from app.persistence.models.product import Product
from app.persistence.models.transaction import SaleEntry


def _clave_de_ajuste(discrepancia_id: uuid.UUID) -> str:
    """Identidad del ajuste que resuelve ESTA discrepancia.

    Es lo que hace que resolver dos veces no duplique la entrada de stock.
    """
    return f"pos-discrepancy:{discrepancia_id}"


def registrar(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    operation_id: uuid.UUID,
    line_id: uuid.UUID,
    sale_entry_id: uuid.UUID | None,
    product_id: uuid.UUID | None,
    pending_units: int,
    stock_at_capture: int,
) -> PosStockDiscrepancy:
    """La salida pendiente de una línea. ``pending_units`` es la cantidad COMPLETA."""
    discrepancia = PosStockDiscrepancy(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        operation_id=operation_id,
        line_id=line_id,
        sale_entry_id=sale_entry_id,
        product_id=product_id,
        pending_units=pending_units,
        stock_at_capture=stock_at_capture,
        status=ESTADO_ABIERTA,
    )
    session.add(discrepancia)
    return discrepancia


async def ventas_con_salida_pendiente(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    product_ids: list[uuid.UUID] | None = None,
) -> set[uuid.UUID]:
    """``sale_entry_id`` de las ventas cuya salida de stock sigue pendiente.

    **Una sola implementación para los DOS chequeos de inventario.** El agregado
    (``inventory_integrity_service``) y el temporal
    (``inventory_temporal_service``) restan las ventas vivas dando por sentado
    que descontaron; una venta capturada todavía no lo hizo, y contarla daría
    una divergencia falsa en los dos. Si cada chequeo tuviera su propia
    consulta, tarde o temprano se separarían y un mismo dato se contaría
    distinto en cada uno.
    """
    stmt = select(PosStockDiscrepancy.sale_entry_id).where(
        PosStockDiscrepancy.tenant_id == tenant_id,
        PosStockDiscrepancy.status == ESTADO_ABIERTA,
        PosStockDiscrepancy.sale_entry_id.isnot(None),
    )
    if product_ids:
        stmt = stmt.where(PosStockDiscrepancy.product_id.in_(product_ids))
    filas = (await session.execute(stmt)).scalars().all()
    return {fila for fila in filas if fila is not None}


async def listar(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    solo_abiertas: bool = True,
    limit: int = 100,
) -> list[tuple[PosStockDiscrepancy, str | None, datetime]]:
    """Las deudas de stock con el producto y la fecha de la venta, más nuevas primero.

    La fecha de la venta —no la de la captura— es lo que le permite al dueño
    ubicar el ticket: una venta de anteayer puede haber sincronizado hoy.
    """
    stmt = (
        select(PosStockDiscrepancy, Product.name, PosOperation.operation_date)
        .join(PosOperation, PosOperation.id == PosStockDiscrepancy.operation_id)
        .outerjoin(Product, Product.id == PosStockDiscrepancy.product_id)
        .where(PosStockDiscrepancy.tenant_id == tenant_id)
    )
    if solo_abiertas:
        stmt = stmt.where(PosStockDiscrepancy.status == ESTADO_ABIERTA)
    stmt = stmt.order_by(PosStockDiscrepancy.created_at.desc()).limit(limit)
    return [(f[0], f[1], f[2]) for f in (await session.execute(stmt)).all()]


async def obtener(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    discrepancia_id: uuid.UUID,
    *,
    para_actualizar: bool = False,
) -> PosStockDiscrepancy | None:
    stmt = select(PosStockDiscrepancy).where(
        PosStockDiscrepancy.tenant_id == tenant_id,
        PosStockDiscrepancy.id == discrepancia_id,
    )
    if para_actualizar:
        stmt = stmt.with_for_update()
    return (await session.execute(stmt)).scalar_one_or_none()


async def aplicar_ajuste_de_entrada(
    session: AsyncSession,
    discrepancia: PosStockDiscrepancy,
    *,
    occurred_at: datetime | None,
) -> int:
    """Entra el faltante y después sale la venta completa. Devuelve el faltante.

    El faltante se mide **ahora**, no en la captura: si en el medio entró una
    compra, el ajuste tiene que ser menor, y si se vendió más, mayor. Con el
    número viejo el stock quedaría mal en los dos casos.
    """
    if discrepancia.product_id is None:
        return 0
    producto = (
        await session.execute(
            select(Product)
            .where(
                Product.id == discrepancia.product_id,
                Product.tenant_id == discrepancia.tenant_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if producto is None:
        raise ValueError("El producto de la discrepancia ya no existe.")

    faltante = max(0, discrepancia.pending_units - producto.stock_units)
    if faltante:
        await stock_service.register_reconciliation_adjustment(
            discrepancia.product_id,
            discrepancia.tenant_id,
            faltante,
            reason="Salida pendiente de caja: la mercadería estaba y el sistema no la tenía.",
            source_event_id=_clave_de_ajuste(discrepancia.id),
            db=session,
            occurred_at=occurred_at,
        )
    if discrepancia.sale_entry_id is not None:
        venta = await session.get(SaleEntry, discrepancia.sale_entry_id)
        if venta is not None:
            # Idempotente por `sale:{id}`: si ya descontó, no vuelve a hacerlo.
            await stock_service.decrement_for_sale(venta, session)
    return faltante


def marcar_resuelta(
    session: AsyncSession,
    discrepancia: PosStockDiscrepancy,
    *,
    resolucion: str,
    reason: str | None,
    user_id: uuid.UUID | None,
    detalle: dict[str, object] | None = None,
) -> None:
    ahora = datetime.now(UTC)
    discrepancia.status = ESTADO_RESUELTA
    discrepancia.resolution = resolucion
    discrepancia.reason = reason
    discrepancia.resolved_at = ahora
    discrepancia.resolved_by_user_id = user_id
    session.add(
        DecisionAuditLog(
            tenant_id=discrepancia.tenant_id,
            decision_type="POS_STOCK_DISCREPANCY_RESOLVED",
            decision_data={
                "discrepancy_id": str(discrepancia.id),
                "operation_id": str(discrepancia.operation_id),
                "product_id": str(discrepancia.product_id),
                "pending_units": discrepancia.pending_units,
                "stock_at_capture": discrepancia.stock_at_capture,
                "resolution": resolucion,
                "reason": reason,
                **(detalle or {}),
            },
            triggered_by="ui:pos",
            actor_user_id=user_id,
            context={"endpoint": "pos.resolve_discrepancy"},
            created_at=ahora,
        )
    )


async def cerrar_por_anulacion(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    operation_id: uuid.UUID,
    user_id: uuid.UUID | None,
) -> int:
    """Anular el ticket resuelve sus deudas de stock: ya no hay salida pendiente.

    Vale venga la anulación de donde venga —la ruta normal o la resolución de la
    discrepancia—, porque si no un ticket anulado dejaría una deuda abierta
    sobre una venta que ya no existe.
    """
    abiertas = (
        (
            await session.execute(
                select(PosStockDiscrepancy).where(
                    PosStockDiscrepancy.tenant_id == tenant_id,
                    PosStockDiscrepancy.operation_id == operation_id,
                    PosStockDiscrepancy.status == ESTADO_ABIERTA,
                )
            )
        )
        .scalars()
        .all()
    )
    for discrepancia in abiertas:
        marcar_resuelta(
            session,
            discrepancia,
            resolucion="anular_ticket",
            reason=discrepancia.reason,
            user_id=user_id,
        )
    return len(abiertas)


__all__ = [
    "RESOLUCION_AJUSTE",
    "aplicar_ajuste_de_entrada",
    "cerrar_por_anulacion",
    "listar",
    "marcar_resuelta",
    "obtener",
    "registrar",
    "ventas_con_salida_pendiente",
]
