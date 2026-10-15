"""Lecturas del historial del costo promedio para costear ventas (M·P3).

Consultas Core (``Select``) que ejecutan tanto el listener del costeo (sesión
sincrónica, dentro del greenlet) como el servicio (``AsyncSession``). Por lote:
una consulta de puntos y una de estados por tenant, nunca una por venta.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import Select, and_, func, or_, select

from app.domain.costo_promedio_de_venta import PuntoDelHistorial
from app.persistence.models.product_cost import ProductCostHistory, ProductCostState


@dataclass
class PuntosDelProducto:
    puntos: list[PuntoDelHistorial] = field(default_factory=list)
    dias: list[date] = field(default_factory=list)


def consulta_estados(
    tenant_id: uuid.UUID, product_ids: Iterable[uuid.UUID], desde: date
) -> Select[Any]:
    """``(product_id, pendiente_desde, reconstruido_en, piso)`` por producto.

    Sin fila o sin ``reconstruido_en``, el historial nunca se publicó (la fila
    puede existir sólo por una marca). ``piso`` es el día del último registro
    con día ≤ ``desde``: el vigente al empezar el rango de las ventas. Es UNA
    subconsulta por producto (``max`` sobre el índice ``(tenant, producto,
    día)``), no por registro.
    """
    s = ProductCostState
    h = ProductCostHistory
    piso = (
        select(func.max(h.dia))
        .where(h.tenant_id == s.tenant_id, h.product_id == s.product_id, h.dia <= desde)
        .correlate(s)
        .scalar_subquery()
    )
    return select(s.product_id, s.pendiente_desde, s.reconstruido_en, piso).where(
        s.tenant_id == tenant_id, s.product_id.in_(list(product_ids))
    )


def consulta_puntos(
    tenant_id: uuid.UUID, pisos: dict[uuid.UUID, date | None], desde: date, hasta: date
) -> Select[Any]:
    """Los registros que pueden cubrir una venta con día en ``[desde, hasta]``.

    Por producto, desde su ``piso`` (o ``desde`` si no tiene registros antes)
    hasta ``hasta``: un rango acotado sobre el índice, sin subconsultas. En el
    orden del historial, así el último de cada día es el estado al cierre.
    """
    h = ProductCostHistory
    rangos = [
        and_(h.product_id == pid, h.dia >= (piso if piso is not None else desde))
        for pid, piso in pisos.items()
    ]
    return (
        select(h.product_id, h.dia, h.costo_promedio, h.estado, h.aproximado, h.motivo)
        .where(h.tenant_id == tenant_id, h.dia <= hasta, or_(*rangos))
        .order_by(h.product_id, h.dia, h.clase_orden, h.fecha, h.creado, h.evento_id)
    )


def puntos_de_filas(filas: Iterable[Any]) -> dict[uuid.UUID, PuntosDelProducto]:
    por_producto: dict[uuid.UUID, PuntosDelProducto] = defaultdict(PuntosDelProducto)
    for pid, dia, promedio, estado, aproximado, motivo in filas:
        p = por_producto[pid]
        p.puntos.append(
            PuntoDelHistorial(
                dia=dia,
                promedio=None if promedio is None else Decimal(promedio),
                estado=estado,
                aproximado=bool(aproximado),
                motivo=motivo,
            )
        )
        p.dias.append(dia)
    return por_producto
