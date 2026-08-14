"""F-H3.d.4 — aplicar al inventario la historia de ventas de un archivo importado.

**F-F.3 — quién llama a esto.** Dos momentos, un solo núcleo:

1. El **confirm**, en una segunda pasada dentro de su savepoint, para las hojas
   resueltas como ``historical_replay``. Hasta F-F.2 no lo hacía: regía la
   decisión de F-H3.c (confirmar → revisar → aplicar), que existía porque el
   replay no se podía validar por fecha. Con el gate cronológico esa condición
   dejó de existir y pedir un segundo clic no compraba nada.
2. El **endpoint** ``POST /ingestion/files/{id}/inventory-replay``, que sigue
   siendo la vía de lo que quedó **pendiente**: el usuario carga el inventario
   que faltaba y vuelve a aplicar, sin volver a importar el archivo.

Los dos entran por ``run_inventory_replay``, y no por una copia adaptada: lo que
se aplica en el confirm y lo que se aplica después tienen que ser la misma
operación, o el segundo intento podría descontar distinto que el primero.

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

**Qué pasa si ya no alcanza el stock.** La venta sin respaldo cuyo producto tiene
saldo conocido ni siquiera entra: la saca el gate al confirmar (F-H3.d.3). La que
llega hasta acá sin unidades es la del producto cuyo saldo NO se sabe (F-F.2) o
la de un stock que se movió entre dos corridas — y en los dos casos ya está en los
libros, así que anularla cambiaría facturación confirmada. Su descuento queda
**pendiente** y se informa: el usuario carga el inventario que falta y vuelve a
aplicar. Es la única salida que no rompe ni el inventario ni la contabilidad.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services import stock_service
from app.application.services.inventory_movement_origin import SOURCE_HISTORICAL_REPLAY
from app.application.services.stock_service import sale_source_event_id
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

#: Claves por query al buscar los descuentos ya vivos. Un archivo NO tiene tope de
#: filas (`file_parsing`: "Sin límite de filas") y Postgres corta en 65.535
#: parámetros por statement: con un archivo de esa escala, un solo ``IN (...)`` con
#: una clave por venta revienta en asyncpg en pleno apply de inventario. Mismo
#: tamaño que el re-apuntado de FKs del dedup
#: (``product_dedup_service.REPOINT_CHUNK_SIZE``).
CLAVES_CHUNK_SIZE = 500


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
    """``source_event_id`` de los descuentos de venta ya vivos, del lote que sea.

    Se consulta por lotes de ``CLAVES_CHUNK_SIZE``: la lista de ventas no tiene
    tope y un ``IN (...)`` con una clave por venta se pasa del límite de binds de
    Postgres justo en los archivos grandes, que son los que más tardan en llegar
    hasta acá. Sin ventas el bucle no itera y no se consulta nada.
    """
    claves = [sale_source_event_id(v.id) for v in ventas]
    encontradas: set[str] = set()
    for inicio in range(0, len(claves), CLAVES_CHUNK_SIZE):
        filas = await session.execute(
            select(InventoryMovement.source_event_id).where(
                InventoryMovement.tenant_id == tenant_id,
                InventoryMovement.movement_type == "sale",
                InventoryMovement.voided_at.is_(None),
                InventoryMovement.source_event_id.in_(
                    claves[inicio : inicio + CLAVES_CHUNK_SIZE]
                ),
            )
        )
        encontradas.update(row[0] for row in filas if row[0])
    return encontradas


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

    # F-F.3.b: por lote, no venta por venta. Aplicar de a una costaba ~4 sentencias
    # y un envío al broker por venta —sobre el archivo real, ~4.700 sentencias
    # adentro del request del confirm—, que es exactamente la demora que F-T existe
    # para no reintroducir. El lote vive en `stock_service` y no acá: lo que aplica
    # el confirm y lo que aplica el reintento del panel tienen que ser la misma
    # operación, y un lote armado del lado del caller volvería a separarlas.
    #
    # Las ventas ya descontadas ni siquiera llegan hasta acá (las sacó el chequeo de
    # `_ya_descontadas`), así que `ya_aplicadas` sólo suma lo que aparezca en la
    # CARRERA: una venta en vivo que se descontó entre aquel SELECT y este INSERT.
    # El lote la resuelve rehaciéndose de a una, que es el camino de siempre.
    bulk = await stock_service.decrement_stock_bulk(
        tenant_id,
        [
            stock_service.BulkDecrementItem(
                product=producto,
                qty=int(venta.quantity or 0),
                source_event_id=sale_source_event_id(venta.id),
                occurred_at=venta.transaction_date,
            )
            for venta, producto in aplicables
        ],
        session,
        source_upload_id=file_id,
        source_type=SOURCE_HISTORICAL_REPLAY,
    )
    resultado.aplicadas += bulk.applied
    resultado.ya_aplicadas += bulk.already_applied

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
