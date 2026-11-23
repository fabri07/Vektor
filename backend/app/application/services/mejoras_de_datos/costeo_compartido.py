"""Piezas que comparten las mejoras que completan el costo de una compra.

``compra_sin_costo`` (ruta A) y ``compra_catalogo_sin_costo`` (ruta B) hacen lo
mismo con el costo una vez que lo tienen: lo escriben en el movimiento, costean YA
las ventas ``sin_costo`` que ese movimiento pasa a respaldar (para capturar su
«antes» y «después») y, al deshacer, restauran el movimiento, el costo vigente del
producto y esas ventas SÓLO si siguen como las dejó la corrida.

Se extrajo sin cambiar comportamiento: el código es el que estaba en
``compra_sin_costo.py``.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.application.services.costeo_retroactivo_service import (
    _columnas_del_snapshot,
    _restaurar,
    _serializar,
)
from app.application.services.mejoras_de_datos.contrato import Reversion
from app.persistence.costeo_de_ventas import (
    _dia_de_negocio,
    costear_ventas,
    ventas_sin_costo_desde,
)
from app.persistence.models.audit import DecisionAuditLog
from app.persistence.models.inventory import InventoryMovement
from app.persistence.models.product import Product
from app.persistence.models.repair import DataRepairItem, DataRepairRun
from app.persistence.models.transaction import SaleEntry


def foto_movimiento(m: InventoryMovement) -> dict[str, Any]:
    return {
        "unit_cost": None if m.unit_cost is None else str(m.unit_cost),
        "unit_cost_final": None if m.unit_cost_final is None else str(m.unit_cost_final),
        "shipping_treatment": m.shipping_treatment,
        "tax_treatment": m.tax_treatment,
    }


def foto_producto(p: Product) -> dict[str, Any]:
    return {
        "unit_cost_ars": None if p.unit_cost_ars is None else str(p.unit_cost_ars),
        "custom_fields": dict(p.custom_fields or {}),
    }


async def costear_ventas_del_movimiento(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    mov: InventoryMovement,
    run: DataRepairRun,
) -> int:
    """Costea YA las ventas ``sin_costo`` que este movimiento hace costeables y
    guarda su antes/después. Devuelve cuántas quedaron costeadas."""
    dia = _dia_de_negocio(mov)
    product_id = mov.product_id

    def _sync(s: Session) -> list[tuple[SaleEntry, dict[str, Any]]]:
        ids = ventas_sin_costo_desde(s, {(tenant_id, product_id): dia})
        if not ids:
            return []
        ventas = list(
            s.execute(
                select(SaleEntry).where(SaleEntry.id.in_(ids), SaleEntry.tenant_id == tenant_id)
            ).scalars()
        )
        antes = {v.id: _serializar(v) for v in ventas}
        costear_ventas(s, dict.fromkeys(ids, True), set())
        return [(v, antes[v.id]) for v in ventas if v.cost_origin != "sin_costo"]

    costeadas = await session.run_sync(_sync)
    for venta, antes in costeadas:
        session.add(
            DataRepairItem(
                run_id=run.id,
                tenant_id=tenant_id,
                sale_entry_id=venta.id,
                product_id=venta.product_id,
                action="UPDATE_SALE",
                before_json=antes,
                after_json=_serializar(venta),
                confidence="MEDIUM",
            )
        )
    await session.flush()
    return len(costeadas)


async def restaurar_costo_de_productos(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    por_producto: dict[str, list[DecisionAuditLog]],
    pendientes_por_producto: set[str],
) -> None:
    """El costo vigente del producto: se restaura sólo si nadie lo tocó desde la
    corrida y TODAS sus compras de esta corrida volvieron atrás."""
    for pid, del_producto in por_producto.items():
        if pid in pendientes_por_producto:
            continue
        producto = (
            await session.execute(
                select(Product)
                .where(Product.id == uuid.UUID(pid), Product.tenant_id == tenant_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if producto is None:
            continue
        inicial = del_producto[0].decision_data["antes"]["producto"]
        final = del_producto[-1].decision_data["despues"]["producto"]
        if foto_producto(producto) == final:
            producto.unit_cost_ars = (
                None if inicial["unit_cost_ars"] is None else Decimal(inicial["unit_cost_ars"])
            )
            producto.custom_fields = dict(inicial["custom_fields"])


async def restaurar_ventas_de_la_corrida(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    run: DataRepairRun,
    ya: set[str],
    pendientes_por_producto: set[str],
    res: Reversion,
) -> None:
    """Las ventas, con el costo del movimiento ya en NULL en este mismo flush: así
    el movimiento deja de ser fuente y el costeo no las vuelve a tomar."""
    items = list(
        (
            await session.execute(
                select(DataRepairItem).where(
                    DataRepairItem.run_id == run.id,
                    DataRepairItem.tenant_id == tenant_id,
                )
            )
        ).scalars()
    )
    columnas = _columnas_del_snapshot()
    for item in items:
        sid = str(item.sale_entry_id)
        if sid in ya:
            continue
        if str(item.product_id) in pendientes_por_producto:
            res.pendientes[sid] = "la compra de su producto no se pudo deshacer"
            continue
        venta = (
            await session.execute(
                select(SaleEntry)
                .where(SaleEntry.id == item.sale_entry_id, SaleEntry.tenant_id == tenant_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if venta is None or _serializar(venta) != item.after_json:
            res.pendientes[sid] = "la venta cambió después de la corrida"
            continue
        for c in columnas:
            setattr(venta, c, _restaurar(c, (item.before_json or {}).get(c)))
        res.revertidos.append(sid)
    await session.flush()
