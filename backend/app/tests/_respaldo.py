"""Un producto con compra previa para los tests que usan una venta como insumo (B·5e).

Desde B·5e toda venta lleva producto y una fuente con costo de fecha ≤ la de la
venta (``respaldo_service.exigir_respaldo``). Los tests que prueban OTRA cosa
(clientes, fiado, idempotencia, arqueo, caja…) y sólo necesitan «una venta»
usan estos helpers en vez de vender en el aire.

La fuente es el stock inicial cargado a mano (``registrar_apertura_manual``): el
MISMO movimiento ``adjustment`` + ``catalog_initial_stock`` que deja la app, con
fecha bien en el pasado para respaldar cualquier venta de los tests.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services import stock_service
from app.domain.contrato_de_costo import ContratoDeCosto
from app.persistence.models.product import Product

#: Lo bastante viejo para respaldar las ventas fechadas de los tests (2020, etc.).
DIA_DE_COMPRA: date = date(2015, 1, 1)


def _dia(dia: date | None) -> date:
    return dia if dia is not None else DIA_DE_COMPRA


def _nombre(nombre: str | None) -> str:
    # Único por defecto: el alta rechaza un homónimo (identidad de producto).
    return nombre if nombre is not None else f"Producto {uuid.uuid4().hex[:8]}"


async def producto_con_compra(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    nombre: str | None = None,
    stock: int = 100,
    costo: Decimal = Decimal("10"),
    precio: Decimal = Decimal("1000"),
    dia: date | None = None,
) -> Product:
    """Producto con ``stock`` unidades de stock inicial al ``costo``, desde ``dia``."""
    product = Product(
        tenant_id=tenant_id, name=_nombre(nombre), sale_price_ars=precio, stock_units=0
    )
    db.add(product)
    await db.flush()
    d = _dia(dia)
    await stock_service.registrar_apertura_manual(
        product.id,
        tenant_id,
        stock,
        ContratoDeCosto(unit_cost_final=costo),
        db,
        fecha=datetime(d.year, d.month, d.day, 9),
        confirmado_por=None,
    )
    await db.commit()
    return product


async def producto_con_compra_http(
    client: AsyncClient,
    headers: dict[str, Any],
    *,
    nombre: str | None = None,
    stock: int = 100,
    costo: str = "10.00",
    precio: str = "1000.00",
    dia: date | None = None,
) -> str:
    """Igual que ``producto_con_compra`` pero por ``POST /products`` (alta con stock
    inicial). Devuelve el id como string, listo para un payload."""
    d = _dia(dia)
    resp = await client.post(
        "/api/v1/products",
        json={
            "name": _nombre(nombre),
            "sale_price_ars": precio,
            "stock_units": stock,
            "unit_cost_ars": costo,
            "acquired_at": datetime(d.year, d.month, d.day, 9).isoformat(),
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


__all__ = ["DIA_DE_COMPRA", "producto_con_compra", "producto_con_compra_http"]
