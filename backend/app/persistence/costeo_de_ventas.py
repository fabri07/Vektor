"""Costeo de ventas al confirmar (M·2a): el snapshot de costo por línea.

Toda venta con producto se costea en la MISMA transacción que la crea, sin que
cada ruta tenga que acordarse. Las rutas son muchas (POS online y sin red,
``/sales``, ``bulk``, ``manual-batch``, el chat, la ingesta plana y multihoja, la
relectura y Otros) y una que no llamara a un servicio dejaría ventas sin costo
en silencio. Por eso son dos listeners globales de ``Session``:

- ``after_flush`` ANOTA en ``session.info`` cada venta nueva con producto y cada
  venta a la que se le cambió el producto o la cantidad (PATCH, relectura).
- ``before_commit`` la COSTEA por lote: una consulta de candidatas para todas las
  fechas, una de productos y una del régimen por tenant. La regla es
  ``domain/costo_de_venta.py``.

Corre en la transacción de la venta, así que ve las compras que se registraron
en ella. Con ``AsyncSession`` los listeners corren dentro del greenlet de
SQLAlchemy y pueden usar la API sincrónica de la sesión.

**No reescribe un snapshot:** una venta ya costeada no se revalúa. Si le cambian
la cantidad, se recalcula el costo de la línea con el MISMO costo unitario. Sólo
si le cambian el producto se vuelve a elegir la fuente. Una venta anulada o sin
producto no se costea.

**No bloquea la venta:** el costeo corre en un SAVEPOINT. Si falla, se registra
el error y la venta se guarda sin snapshot (NULL = sin costear), que es lo mismo
que una venta vieja y lo completa el script de M·2c. El cliente nunca manda
costo: lo calcula el servidor.

``tenant_id`` sale de cada venta (que la ruta creó con el del JWT): las
candidatas, los productos y el régimen se buscan con ese tenant.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Iterable
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Final

from sqlalchemy import event, inspect, select
from sqlalchemy.orm import Session

from app.domain.costo_de_referencia import CompraCandidata, costo_de_referencia
from app.domain.costo_de_venta import (
    MOTIVO_UNIDAD_DUDOSA,
    VERSION_SNAPSHOT,
    CostoDeVenta,
    costo_de_venta,
)
from app.domain.costo_promedio_de_venta import (
    CostoPromedioDeVenta,
    costo_promedio_de_venta,
    punto_a,
)
from app.domain.costo_vigente import (
    CATALOGO_FIELD,
    FECHA_FIELD,
    MANUAL_FIELD,
    leer_costo_fechado,
)
from app.domain.sale_unit import importe
from app.observability.logger import get_logger
from app.persistence.models.business import BusinessProfile
from app.persistence.models.product import Product
from app.persistence.models.transaction import SaleEntry
from app.persistence.repositories.costo_de_referencia_repository import (
    candidata_de_fila,
    consulta_todas_las_candidatas,
)
from app.persistence.repositories.costo_promedio_de_venta_repository import (
    consulta_estados,
    consulta_puntos,
    puntos_de_filas,
)

logger = get_logger(__name__)

#: ``session.info[_PENDIENTES]`` = ``{sale_id: repreciar}``. ``repreciar`` True
#: obliga a elegir la fuente de nuevo (venta nueva o con otro producto).
_PENDIENTES: Final = "m2a_ventas_por_costear"


#: ``session.info[_UNIDAD_DUDOSA]`` = ventas cuya ruta NO garantiza que
#: ``quantity`` esté en unidades base (``POST /sales``, ``/sales/bulk``, chat).
_UNIDAD_DUDOSA: Final = "m2a_ventas_unidad_dudosa"


_COLUMNAS_DEL_SNAPSHOT: Final = (
    "cost_unit_ars", "cost_ars", "cost_confirmed", "cost_origin", "cost_reason",
    "cost_snapshot_version",
    # M·P3: sin producto tampoco hay costo promedio.
    "cost_avg_unit_ars", "cost_avg_ars", "cost_avg_estado", "cost_avg_aproximado",
    "cost_avg_motivo", "cost_avg_pendiente",
)


def marcar_unidad_no_confirmada(info: dict[Any, Any], sale_ids: Iterable[uuid.UUID]) -> None:
    """Las rutas que guardan ``quantity`` tal como llega lo avisan acá (``info`` de
    la sesión). Con un producto de factor distinto de 1 la venta queda
    ``sin_costo`` con su motivo: un costo dividido por un factor que la cantidad
    quizá no tiene sería un número inventado."""
    info.setdefault(_UNIDAD_DUDOSA, set()).update(sale_ids)


def _anotar(session: Session, sale_id: uuid.UUID, repreciar: bool) -> None:
    pendientes: dict[uuid.UUID, bool] = session.info.setdefault(_PENDIENTES, {})
    pendientes[sale_id] = pendientes.get(sale_id, False) or repreciar


def _limpiar_snapshot(venta: SaleEntry) -> None:
    for columna in _COLUMNAS_DEL_SNAPSHOT:
        setattr(venta, columna, None)


@event.listens_for(Session, "after_flush")
def _anotar_ventas(session: Session, _flush_context: Any) -> None:
    for obj in session.new:
        if (
            isinstance(obj, SaleEntry)
            and obj.product_id is not None
            and obj.cost_snapshot_version is None
        ):
            _anotar(session, obj.id, True)
    for obj in session.dirty:
        if not isinstance(obj, SaleEntry):
            continue
        estado = inspect(obj)
        if obj.product_id is None:
            # Le sacaron el producto: sin producto no hay costo. Se limpia al
            # costear (un cambio hecho DENTRO de ``after_flush`` se pierde).
            if obj.cost_snapshot_version is not None:
                _anotar(session, obj.id, True)
            continue
        if (
            estado.attrs.product_id.history.has_changes()
            or estado.attrs.transaction_date.history.has_changes()
        ):
            # Otro producto u otro día: la fuente elegible es otra.
            _anotar(session, obj.id, True)
        elif estado.attrs.quantity.history.has_changes():
            _anotar(session, obj.id, obj.cost_snapshot_version is None)


@event.listens_for(Session, "after_soft_rollback")
def _olvidar_al_revertir(session: Session, previous_transaction: Any) -> None:
    if previous_transaction.parent is None:
        session.info.pop(_PENDIENTES, None)
        session.info.pop(_UNIDAD_DUDOSA, None)


# ``insert=True``: este ``before_commit`` corre PRIMERO, se importe este módulo
# antes o después que ``costo_promedio_pendiente``. Si las marcas se escriben
# antes, el upsert de ``product_cost_state`` queda tomado mientras se costea (un
# import de 5.000 ventas: ~4 s) y la venta del POS que marca el mismo producto
# espera todo eso (bench M·P3, medición 5). El orden de registro dependía del
# orden de los imports del proceso (API, worker, scripts).
@event.listens_for(Session, "before_commit", insert=True)
def _costear_al_confirmar(session: Session) -> None:
    # ``before_commit`` también se despacha al LIBERAR un savepoint
    # (``begin_nested``/``guarded_savepoint``). Ahí no se costea: el snapshot no
    # puede depender de cuándo se libera un savepoint ajeno ni perderse si el
    # savepoint de afuera se revierte. Sólo el commit de nivel superior.
    if session.in_nested_transaction():
        return
    # Corre ANTES del flush final del commit: sin este flush, una venta agregada
    # sin flushear todavía no está anotada.
    session.flush()
    pendientes: dict[uuid.UUID, bool] = session.info.pop(_PENDIENTES, {})
    dudosas: set[uuid.UUID] = session.info.pop(_UNIDAD_DUDOSA, set())
    if pendientes:
        costear_ventas(session, pendientes, dudosas)


def costear_ventas(
    session: Session, pendientes: dict[uuid.UUID, bool], dudosas: set[uuid.UUID] | None = None
) -> None:
    """Escribe el snapshot de las ventas anotadas (API sincrónica de la sesión).

    Cada tenant en su SAVEPOINT: si el costeo de uno falla, se registra y sus
    ventas quedan sin snapshot (NULL = sin costear), sin arrastrar a las demás.
    """
    ventas = (
        session.execute(select(SaleEntry).where(SaleEntry.id.in_(list(pendientes))))
        .scalars()
        .all()
    )
    por_tenant: dict[uuid.UUID, list[SaleEntry]] = defaultdict(list)
    for venta in ventas:
        if venta.product_id is None:
            _limpiar_snapshot(venta)
            continue
        if venta.voided_at is not None:
            continue
        por_tenant[venta.tenant_id].append(venta)
    for tenant_id, del_tenant in por_tenant.items():
        try:
            with session.begin_nested():
                _costear_del_tenant(session, tenant_id, del_tenant, pendientes, dudosas or set())
        except Exception:  # noqa: BLE001 — la venta se guarda igual, sin costear
            logger.exception(
                "costeo_de_ventas_fallo", tenant_id=str(tenant_id), ventas=len(del_tenant)
            )


def _dia(venta: SaleEntry) -> date:
    # ``transaction_date`` es hora argentina sin zona: su fecha ES el día de negocio.
    # ``/sales/bulk`` lo asigna como ``date`` y el objeto en memoria lo conserva así.
    valor = venta.transaction_date
    return valor.date() if isinstance(valor, datetime) else valor


def _costear_del_tenant(
    session: Session,
    tenant_id: uuid.UUID,
    ventas: list[SaleEntry],
    pendientes: dict[uuid.UUID, bool],
    dudosas: set[uuid.UUID],
) -> None:
    product_ids = {v.product_id for v in ventas if v.product_id is not None}
    productos = {
        p.id: p
        for p in session.execute(
            select(Product).where(Product.tenant_id == tenant_id, Product.id.in_(product_ids))
        ).scalars()
    }

    a_elegir: list[SaleEntry] = []
    for venta in ventas:
        producto = productos.get(venta.product_id)  # type: ignore[arg-type]
        if producto is None:
            continue
        ya_costeada = venta.cost_snapshot_version is not None
        if ya_costeada and not pendientes.get(venta.id, False):
            # Cambió la cantidad: mismo costo unitario, nueva línea.
            if venta.cost_unit_ars is not None:
                venta.cost_ars = importe(
                    venta.cost_unit_ars, venta.quantity, producto.base_units_per_sale_unit
                )
            continue
        a_elegir.append(venta)
    if a_elegir:
        _elegir_fuente(session, tenant_id, a_elegir, productos, dudosas)
    # M·P3, DESPUÉS del snapshot (usa su motivo) y en su propio SAVEPOINT: si el
    # costo promedio falla, el costo principal ya calculado no se pierde.
    try:
        with session.begin_nested():
            _costear_promedio(session, tenant_id, ventas, productos, dudosas)
    except Exception:  # noqa: BLE001 — el costo promedio es un dato: la venta sigue
        logger.exception("costo_promedio_de_venta_fallo", tenant_id=str(tenant_id))


def _elegir_fuente(
    session: Session,
    tenant_id: uuid.UUID,
    a_elegir: list[SaleEntry],
    productos: dict[uuid.UUID, Product],
    dudosas: set[uuid.UUID],
) -> None:
    """El snapshot de M·2a: elige la fuente del costo principal a la fecha."""
    fiscal_crudo = session.execute(
        select(BusinessProfile.fiscal_condition)
        .where(BusinessProfile.tenant_id == tenant_id)
        .limit(1)
    ).scalar_one_or_none()
    hasta = max(_dia(v) for v in a_elegir)
    candidatas: dict[uuid.UUID, list[CompraCandidata]] = defaultdict(list)
    for fila in session.execute(
        consulta_todas_las_candidatas(tenant_id, {v.product_id for v in a_elegir}, hasta)  # type: ignore[misc]
    ):
        candidatas[fila.product_id].append(candidata_de_fila(fila))

    for venta in a_elegir:
        producto = productos[venta.product_id]  # type: ignore[index]
        dia = _dia(venta)
        if venta.id in dudosas and producto.base_units_per_sale_unit != 1:
            snapshot = CostoDeVenta(None, None, False, "sin_costo", MOTIVO_UNIDAD_DUDOSA)
        else:
            referencia = costo_de_referencia(
                candidatas.get(producto.id, []), dia, _costo_cargado_a(producto, dia)
            )
            snapshot = costo_de_venta(
                referencia,
                leer_costo_fechado(producto.custom_fields, MANUAL_FIELD),
                leer_costo_fechado(producto.custom_fields, CATALOGO_FIELD),
                dia_de_venta=dia,
                unidades_base=venta.quantity,
                base_units_per_sale_unit=producto.base_units_per_sale_unit,
                fiscal_condition_crudo=fiscal_crudo,
            )
        venta.cost_unit_ars = snapshot.costo_unitario
        venta.cost_ars = snapshot.costo_linea
        venta.cost_confirmed = snapshot.confirmado
        venta.cost_origin = snapshot.origen
        venta.cost_reason = snapshot.motivo[:200] if snapshot.motivo else None
        venta.cost_snapshot_version = VERSION_SNAPSHOT


def _costear_promedio(
    session: Session,
    tenant_id: uuid.UUID,
    ventas: list[SaleEntry],
    productos: dict[uuid.UUID, Product],
    dudosas: set[uuid.UUID],
) -> None:
    """M·P3: el costo PROMEDIO de cada venta, al lado del snapshot (que no toca).

    Lee el historial publicado del día de la venta. Si el producto se va a
    recalcular (una marca de esta transacción o un ``pendiente_desde`` que
    alcanza ese día), guarda igual el último publicado con
    ``cost_avg_pendiente = true``: la publicación lo corrige y la UI nunca
    muestra «recalculando» (decisión del dueño). Un producto cuyo historial
    nunca se publicó queda sin valores y pendiente (su venta siempre marca).
    """
    # Tarde y no arriba: importarlo al cargar este módulo registraría sus
    # listeners ANTES que los del costeo y cambiaría el orden de los
    # ``before_commit`` (``models/__init__`` importa el costeo primero).
    from app.persistence.costo_promedio_pendiente import marcas_en_curso  # noqa: PLC0415

    con_producto = [v for v in ventas if v.product_id in productos]
    if not con_producto:
        return
    pids = {v.product_id for v in con_producto if v.product_id is not None}
    dias = [_dia(v) for v in con_producto]
    desde, hasta = min(dias), max(dias)
    estados: dict[uuid.UUID, tuple[date | None, bool]] = {}
    pisos: dict[uuid.UUID, date | None] = {}
    for pid, pendiente, reconstruido, piso in session.execute(
        consulta_estados(tenant_id, pids, desde)
    ).tuples():
        estados[pid] = (pendiente, reconstruido is not None)
        if reconstruido is not None:
            pisos[pid] = piso
    puntos = (
        puntos_de_filas(
            session.execute(consulta_puntos(tenant_id, pisos, desde, hasta)).tuples()
        )
        if pisos
        else {}
    )
    marcas = marcas_en_curso(session)
    for venta in con_producto:
        pid = venta.product_id
        assert pid is not None
        producto = productos[pid]
        dia = _dia(venta)
        marca = marcas.get((tenant_id, pid))
        pendiente_desde, calculado = estados.get(pid, (None, False))
        pendiente = (
            not calculado
            or (pendiente_desde is not None and pendiente_desde <= dia)
            or (marca is not None and marca <= dia)
        )
        if not calculado:
            valores = CostoPromedioDeVenta(None, None, None, None, None)
        else:
            del_producto = puntos.get(pid)
            valores = costo_promedio_de_venta(
                punto_a(del_producto.puntos, del_producto.dias, dia) if del_producto else None,
                unidades_base=venta.quantity,
                base_units_per_sale_unit=producto.base_units_per_sale_unit,
                # El MISMO criterio que la publicación: la marca de esta
                # transacción o el motivo que dejó el snapshot.
                unidad_dudosa=(
                    venta.id in dudosas or venta.cost_reason == MOTIVO_UNIDAD_DUDOSA
                ),
                motivo_dudosa=MOTIVO_UNIDAD_DUDOSA,
            )
        for columna, valor in valores.columnas(pendiente=pendiente).items():
            setattr(venta, columna, valor)


def _costo_cargado_a(producto: Product, dia: date) -> Decimal | None:
    """El costo cargado como respaldo SIN fecha, salvo que su procedencia diga que
    es POSTERIOR a la venta (M·1c lo fijó desde una compra o edición de después):
    eso no es una estimación de lo que valía, es un dato del futuro."""
    sellada = (producto.custom_fields or {}).get(FECHA_FIELD)
    if sellada:
        try:
            if datetime.fromisoformat(str(sellada)).date() > dia:
                return None
        except ValueError:
            pass
    return producto.unit_cost_ars
