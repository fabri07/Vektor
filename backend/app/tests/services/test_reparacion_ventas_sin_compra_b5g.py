"""B·5g: reparación explícita de ventas históricas sin compra previa.

Un negocio con tres ventas viejas (cargadas antes de B·5e):
- 1.000 de «Yerba», con stock inicial desde antes → no se toca;
- 2.000 de «Vela», sin ninguna compra, que descontó 2 unidades → a «Otros»;
- 3.000 sin producto → a «Otros».

``--apply`` deja 1 venta viva (ingresos 6.000 → 1.000), dos filas en «Otros»
y la Vela con sus 2 unidades de vuelta. ``--revert`` las restaura; una que el
usuario ya resolvió desde «Otros» queda como está.
"""

from __future__ import annotations

import unittest.mock
import uuid
from datetime import datetime, time, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services import stock_service
from app.application.services.reparacion_ventas_sin_compra_service import (
    VOID_REASON,
    analizar,
    reparar,
    revertir,
)
from app.domain.business_time import today_ar
from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import SaleEntry
from app.persistence.models.unclassified_record import UnclassifiedRecord
from app.tests._respaldo import producto_con_compra

_DIA = today_ar() - timedelta(days=10)


@pytest.fixture(autouse=True)
def _sin_eventos() -> Any:
    with unittest.mock.patch("app.application.services.stock_service.EventBus.emit"):
        yield


async def _venta(db: AsyncSession, tid: uuid.UUID, monto: str, pid: Any = None) -> SaleEntry:
    v = SaleEntry(
        tenant_id=tid, amount=Decimal(monto), quantity=2, product_id=pid,
        transaction_date=datetime.combine(_DIA, time(12)), payment_method="cash",
        provenance="REAL",
    )
    db.add(v)
    await db.flush()
    if pid is not None:
        await stock_service.decrement_for_sale(v, db)
    await db.commit()
    return v


async def _escenario(
    db: AsyncSession, tid: uuid.UUID
) -> tuple[SaleEntry, SaleEntry, SaleEntry, Product]:
    yerba = await producto_con_compra(db, tid, nombre="Yerba")
    vela = Product(tenant_id=tid, name="Vela", sale_price_ars=Decimal("1000"), stock_units=10)
    db.add(vela)
    await db.commit()
    ok = await _venta(db, tid, "1000", yerba.id)
    sin_compra = await _venta(db, tid, "2000", vela.id)
    sin_producto = await _venta(db, tid, "3000")
    return ok, sin_compra, sin_producto, vela


async def test_analizar_no_toca_nada(db_session: AsyncSession, sample_tenant: Tenant) -> None:
    tid = sample_tenant.tenant_id
    _ok, sin_compra, sin_producto, _vela = await _escenario(db_session, tid)
    esperadas = {sin_compra.id, sin_producto.id}
    r = await analizar(db_session, tid)
    ids, monto = {c.venta.id for c in r.candidatas}, r.monto
    await db_session.rollback()
    assert ids == esperadas
    assert monto == Decimal("5000")
    assert (await db_session.execute(select(UnclassifiedRecord))).first() is None


async def test_apply_y_revert(db_session: AsyncSession, sample_tenant: Tenant) -> None:
    tid = sample_tenant.tenant_id
    ok, sin_compra, sin_producto, vela = await _escenario(db_session, tid)
    await db_session.refresh(vela)
    assert vela.stock_units == 8

    r = await reparar(db_session, tid)
    await db_session.commit()
    vivas = (
        await db_session.execute(select(SaleEntry).where(SaleEntry.voided_at.is_(None)))
    ).scalars().all()
    assert [v.id for v in vivas] == [ok.id]
    await db_session.refresh(sin_compra)
    assert sin_compra.void_reason == VOID_REASON
    otros = (await db_session.execute(select(UnclassifiedRecord))).scalars().all()
    assert sorted(Decimal(o.row_data["monto"]) for o in otros) == [Decimal("2000"), Decimal("3000")]
    assert all(o.suggested_entity == "sale" for o in otros)
    await db_session.refresh(vela)
    assert vela.stock_units == 10  # las 2 unidades vuelven

    # El usuario resuelve una desde «Otros» antes del revert: esa no se toca.
    resuelta = next(o for o in otros if Decimal(o.row_data["monto"]) == Decimal("3000"))
    resuelta.status = "IMPORTED"
    await db_session.commit()

    assert r.run_id is not None
    rev = await revertir(db_session, r.run_id)
    await db_session.commit()
    assert rev.restauradas == [str(sin_compra.id)]
    assert rev.resueltas_por_el_usuario == [str(sin_producto.id)]
    await db_session.refresh(sin_compra)
    assert sin_compra.voided_at is None
    assert not sin_compra.void_reason
    await db_session.refresh(vela)
    assert vela.stock_units == 8
    quedan = (await db_session.execute(select(UnclassifiedRecord))).scalars().all()
    assert [o.id for o in quedan] == [resuelta.id]
