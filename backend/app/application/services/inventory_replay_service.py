"""F-H3.d.4 — aplicar al inventario la historia de ventas de un archivo importado.

El confirm **no** toca stock (decisión de F-H3.c: confirmar → revisar → aplicar).
Este servicio es el segundo paso: descuenta las ventas que el archivo trajo, por
hoja, cuando el usuario lo pide.

**El número se recalcula acá adentro, nunca se lee del confirm.** Entre confirmar
y aplicar pueden haber pasado ventas en vivo, otro import o una corrección
manual. Mostrar el número viejo para una operación que va a escribir otro es
exactamente lo que ya se pagó en el borrado por procedencia — por eso ahí el
DELETE recalcula y su resultado es el autoritativo, no el del preview. Acá igual:
``preview`` y ``apply`` corren la MISMA función y el resultado del apply manda.

**Idempotencia sin código nuevo.** El movimiento lleva ``source_event_id =
"sale:{id}"``, la misma clave que usa el descuento de una venta en vivo. El índice
único parcial de `20260729_0001` hace el resto: aplicar dos veces no descuenta dos
veces, y una venta ya descontada en vivo no se vuelve a descontar acá (**V13**).

**La reversa sale gratis.** El movimiento lleva ``source_upload_id``, así que
borrar el archivo lo voidea con todo lo demás que ese archivo creó, incremental
(**V15**).

**Qué pasa si ya no alcanza el stock.** Al confirmar, una venta sin respaldo ni
siquiera entra (F-H3.d.3). Acá la venta YA está en los libros y anularla cambiaría
facturación confirmada, así que su descuento queda **pendiente** y se informa: el
usuario carga el inventario que falta y vuelve a aplicar. Es la única salida que
no rompe ni el inventario ni la contabilidad.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services import stock_service
from app.application.services._savepoint import SavepointConflictError, guarded_savepoint
from app.application.services.inventory_movement_origin import SOURCE_HISTORICAL_REPLAY
from app.application.services.stock_service import (
    LIVE_SALE_EVENT_CONFLICT,
    sale_source_event_id,
)
from app.domain.inventory_effect import IMPORT_CONTEXT_FIELD
from app.domain.inventory_projection import (
    ImportImpact,
    ProductProjection,
    project_import_impact,
)
from app.domain.inventory_replay_gate import ReplayRow, rows_without_stock_backing
from app.observability.logger import get_logger
from app.persistence.models.inventory import InventoryMovement
from app.persistence.models.product import Product
from app.persistence.models.transaction import SaleEntry

logger = get_logger(__name__)

#: Hoja de las ventas importadas antes de que el import estampara su contexto
#: (F-H3.d.2). No es un context_id real: marca que de esas filas no se sabe de qué
#: hoja vinieron, y por eso el alcance del apply deja de ser por hoja.
CONTEXTO_DESCONOCIDO = "__sin_hoja__"


@dataclass
class VentaPendiente:
    """Una venta cuyo descuento no se pudo aplicar, con el motivo."""

    sale_id: uuid.UUID
    product_id: uuid.UUID
    product_name: str
    quantity: int
    disponible: int


@dataclass
class ReplayOutcome:
    """Resultado de calcular (y opcionalmente aplicar) el replay de un archivo."""

    aplicadas: int = 0
    #: Ventas que YA estaban descontadas cuando se corrió (aplicar de nuevo, o una
    #: venta que se descontó en vivo). No son un error: son el no-op idempotente.
    ya_aplicadas: int = 0
    sin_stock: list[VentaPendiente] = field(default_factory=list)
    impacto: ImportImpact = field(default_factory=lambda: project_import_impact({}))
    #: Hojas que el apply tocó. `CONTEXTO_DESCONOCIDO` = ventas sin hoja registrada.
    hojas: list[str] = field(default_factory=list)
    #: `False` cuando alguna venta del archivo no sabe de qué hoja vino: el alcance
    #: real fue el archivo entero. Se informa; un alcance distinto del declarado que
    #: no se dice se lee como el declarado.
    alcance_por_hoja: bool = True


def _contexto_de(venta: SaleEntry) -> str:
    return str((venta.custom_fields or {}).get(IMPORT_CONTEXT_FIELD) or CONTEXTO_DESCONOCIDO)


async def _ventas_del_archivo(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    file_id: uuid.UUID,
) -> list[SaleEntry]:
    """TODAS las ventas vivas del archivo que pueden mover inventario.

    El filtro por hoja lo aplica el caller sobre esta lista, no esta query: el
    aviso de alcance necesita saber si el archivo tiene ventas sin hoja
    registrada, y eso se pierde si el filtro corre antes.
    """
    ventas = list(
        (
            await session.execute(
                select(SaleEntry).where(
                    SaleEntry.tenant_id == tenant_id,
                    SaleEntry.source_upload_id == file_id,
                    SaleEntry.voided_at.is_(None),
                    SaleEntry.product_id.is_not(None),
                )
            )
        )
        .scalars()
        .all()
    )
    return ventas


async def _ya_descontadas(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    ventas: list[SaleEntry],
) -> set[str]:
    """``source_event_id`` de los descuentos de venta ya vivos, del lote que sea."""
    if not ventas:
        return set()
    claves = [sale_source_event_id(v.id) for v in ventas]
    filas = await session.execute(
        select(InventoryMovement.source_event_id).where(
            InventoryMovement.tenant_id == tenant_id,
            InventoryMovement.movement_type == "sale",
            InventoryMovement.voided_at.is_(None),
            InventoryMovement.source_event_id.in_(claves),
        )
    )
    return {row[0] for row in filas if row[0]}


async def run_inventory_replay(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    file_id: uuid.UUID,
    *,
    context_ids: list[str] | None = None,
    apply: bool = False,
) -> ReplayOutcome:
    """Calcula el replay del archivo y, con ``apply``, lo escribe.

    Una sola función para las dos cosas a propósito: si el preview corriera por su
    lado, lo que se muestra y lo que se aplica podrían separarse con el tiempo, que
    es justo lo que hay que evitar en una operación que mueve inventario.
    """
    # `alcance_por_hoja` se evalúa sobre TODAS las ventas del archivo, no sobre las
    # filtradas: si se mirara la lista ya filtrada, una venta sin hoja registrada
    # quedaría fuera del filtro y el aviso —que existe justamente para decir "hay
    # ventas cuyo origen no sé"— no se dispararía nunca en el único caso donde
    # importa, que es cuando el usuario eligió hojas.
    todas = await _ventas_del_archivo(session, tenant_id, file_id)
    ventas = (
        todas
        if context_ids is None
        else [v for v in todas if _contexto_de(v) in set(context_ids)]
    )
    resultado = ReplayOutcome(hojas=sorted({_contexto_de(v) for v in ventas}))
    resultado.alcance_por_hoja = CONTEXTO_DESCONOCIDO not in {
        _contexto_de(v) for v in todas
    }
    if not ventas:
        return resultado

    descontadas = await _ya_descontadas(session, tenant_id, ventas)
    # Las dos exclusiones se cuentan por separado: una venta ya descontada es el
    # no-op idempotente y se informa; una de cantidad 0 simplemente no habla de
    # unidades y no es "ya aplicada" — meterlas en el mismo contador haría que el
    # resumen dijera que se aplicó algo que nunca iba a aplicarse.
    pendientes: list[SaleEntry] = []
    for venta in ventas:
        if sale_source_event_id(venta.id) in descontadas:
            resultado.ya_aplicadas += 1
        elif int(venta.quantity or 0) > 0:
            pendientes.append(venta)
    if not pendientes:
        return resultado

    productos: dict[uuid.UUID, Product] = {}
    for pid in {v.product_id for v in pendientes if v.product_id is not None}:
        producto = await session.get(Product, pid)
        if producto is not None and producto.tenant_id == tenant_id:
            productos[pid] = producto

    # El saldo de partida es el de AHORA, no el que devolvió el confirm.
    saldos = {pid: int(p.stock_units) for pid, p in productos.items()}
    # Sólo las ventas cuyo producto está resuelto y es del tenant. Las demás no se
    # evalúan ni se aplican: sin producto no hay unidades que mover. Se acarrea el
    # `Product` al lado de la venta en vez de re-buscarlo por id en cada paso.
    con_producto: list[tuple[SaleEntry, Product]] = [
        (v, productos[v.product_id])
        for v in pendientes
        if v.product_id is not None and v.product_id in productos
    ]
    filas = [
        ReplayRow(
            key=venta.id,
            product_id=producto.id,
            day=venta.transaction_date.date(),
            qty=int(venta.quantity or 0),
        )
        for venta, producto in con_producto
    ]
    sin_respaldo = {r.key: r for r in rows_without_stock_backing(filas, saldos)}

    aplicables: list[tuple[SaleEntry, Product]] = []
    for venta, producto in con_producto:
        faltante = sin_respaldo.get(venta.id)
        if faltante is not None:
            resultado.sin_stock.append(
                VentaPendiente(
                    sale_id=venta.id,
                    product_id=producto.id,
                    product_name=producto.name,
                    quantity=faltante.qty,
                    disponible=faltante.disponible,
                )
            )
            continue
        aplicables.append((venta, producto))

    # Impacto de lo que se VA a aplicar (o de lo que se aplicó): saldo de hoy →
    # ventas → saldo final. Las compras del archivo ya están adentro del saldo de
    # hoy (V16), así que listarlas acá las contaría dos veces.
    proyecciones: dict[uuid.UUID, ProductProjection] = {}
    for venta, producto in aplicables:
        proyeccion = proyecciones.get(producto.id)
        if proyeccion is None:
            proyeccion = ProductProjection(
                product_id=producto.id,
                product_name=producto.name,
                saldo_previo=saldos[producto.id],
            )
            proyecciones[producto.id] = proyeccion
        proyeccion.agregar_venta(venta.transaction_date.date(), int(venta.quantity or 0))
    resultado.impacto = project_import_impact(proyecciones)

    if not apply:
        return resultado

    for venta, producto in aplicables:
        try:
            async with guarded_savepoint(session, LIVE_SALE_EVENT_CONFLICT):
                await stock_service.decrement_stock(
                    product_id=producto.id,
                    tenant_id=tenant_id,
                    qty=int(venta.quantity or 0),
                    source_event_id=sale_source_event_id(venta.id),
                    db=session,
                    occurred_at=venta.transaction_date,
                    source_upload_id=file_id,
                    source_type=SOURCE_HISTORICAL_REPLAY,
                )
            resultado.aplicadas += 1
        except SavepointConflictError:
            # La otra rama (una venta en vivo del mismo registro) ya lo creó: no-op
            # idempotente, no un error. Ver `decrement_for_sale`.
            resultado.ya_aplicadas += 1

    logger.info(
        "ingestion.inventory_replay.applied",
        file_id=str(file_id),
        aplicadas=resultado.aplicadas,
        ya_aplicadas=resultado.ya_aplicadas,
        sin_stock=len(resultado.sin_stock),
    )
    return resultado


def outcome_as_dict(outcome: ReplayOutcome) -> dict[str, Any]:
    """Forma serializable, para la respuesta HTTP y la traza."""
    return {
        "aplicadas": outcome.aplicadas,
        "ya_aplicadas": outcome.ya_aplicadas,
        "alcance_por_hoja": outcome.alcance_por_hoja,
        "hojas": outcome.hojas,
        "sin_stock": [
            {
                "sale_id": str(p.sale_id),
                "product_id": str(p.product_id),
                "product_name": p.product_name,
                "quantity": p.quantity,
                "disponible": p.disponible,
            }
            for p in outcome.sin_stock
        ],
        "impacto": [p.as_dict() for p in outcome.impacto.productos],
    }
