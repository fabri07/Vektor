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
from datetime import date
from typing import Any, Final

from sqlalchemy import event, inspect, select
from sqlalchemy.orm import Session

from app.domain.costo_de_referencia import CompraCandidata, costo_de_referencia
from app.domain.costo_de_venta import VERSION_SNAPSHOT, costo_de_venta
from app.domain.costo_vigente import CATALOGO_FIELD, MANUAL_FIELD, leer_costo_fechado
from app.domain.sale_unit import importe
from app.observability.logger import get_logger
from app.persistence.models.business import BusinessProfile
from app.persistence.models.product import Product
from app.persistence.models.transaction import SaleEntry
from app.persistence.repositories.costo_de_referencia_repository import (
    candidata_de_fila,
    consulta_todas_las_candidatas,
)

logger = get_logger(__name__)

#: ``session.info[_PENDIENTES]`` = ``{sale_id: repreciar}``. ``repreciar`` True
#: obliga a elegir la fuente de nuevo (venta nueva o con otro producto).
_PENDIENTES: Final = "m2a_ventas_por_costear"


def _anotar(session: Session, sale_id: uuid.UUID, repreciar: bool) -> None:
    pendientes: dict[uuid.UUID, bool] = session.info.setdefault(_PENDIENTES, {})
    pendientes[sale_id] = pendientes.get(sale_id, False) or repreciar


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
        if not isinstance(obj, SaleEntry) or obj.product_id is None:
            continue
        estado = inspect(obj)
        if estado.attrs.product_id.history.has_changes():
            _anotar(session, obj.id, True)
        elif estado.attrs.quantity.history.has_changes():
            _anotar(session, obj.id, obj.cost_snapshot_version is None)


@event.listens_for(Session, "before_commit")
def _costear_al_confirmar(session: Session) -> None:
    # ``before_commit`` corre ANTES del flush final del commit: sin este flush, una
    # venta agregada sin flushear todavía no está anotada.
    session.flush()
    pendientes: dict[uuid.UUID, bool] = session.info.pop(_PENDIENTES, {})
    if not pendientes:
        return
    try:
        with session.begin_nested():
            costear_ventas(session, pendientes)
    except Exception:  # noqa: BLE001 — la venta se guarda igual, sin costear
        logger.exception("costeo_de_ventas_fallo", ventas=len(pendientes))


def costear_ventas(session: Session, pendientes: dict[uuid.UUID, bool]) -> None:
    """Escribe el snapshot de las ventas anotadas (API sincrónica de la sesión)."""
    ventas = (
        session.execute(select(SaleEntry).where(SaleEntry.id.in_(list(pendientes))))
        .scalars()
        .all()
    )
    por_tenant: dict[uuid.UUID, list[SaleEntry]] = defaultdict(list)
    for venta in ventas:
        if venta.product_id is None or venta.voided_at is not None:
            continue
        por_tenant[venta.tenant_id].append(venta)
    for tenant_id, del_tenant in por_tenant.items():
        _costear_del_tenant(session, tenant_id, del_tenant, pendientes)


def _dia(venta: SaleEntry) -> date:
    # ``transaction_date`` es hora argentina sin zona: su fecha ES el día de negocio.
    return venta.transaction_date.date()


def _costear_del_tenant(
    session: Session,
    tenant_id: uuid.UUID,
    ventas: list[SaleEntry],
    pendientes: dict[uuid.UUID, bool],
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
    if not a_elegir:
        return

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
        referencia = costo_de_referencia(
            candidatas.get(producto.id, []), dia, producto.unit_cost_ars
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
