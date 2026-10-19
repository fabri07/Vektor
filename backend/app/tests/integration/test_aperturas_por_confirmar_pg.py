"""M·2d — el aviso del stock inicial contra PostgreSQL real.

El día de negocio de una apertura sale de ``COALESCE(occurred_at, created_at)``
(hora argentina rotulada UTC). Con ``date()`` en SQL, Postgres lo calcula en el
``TimeZone`` de la sesión y una apertura de las 00:30 caía el día anterior. El
resultado no puede depender de esa zona. Todo dentro de una transacción que se
revierte. Correr con ``-n 0``.
"""

from __future__ import annotations

import contextlib
import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.application.services.facts_provider import aperturas_por_confirmar
from app.application.services.inventory_movement_origin import SOURCE_CATALOG_INITIAL_STOCK
from app.persistence.models.inventory import InventoryMovement
from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import SaleEntry

TEST_PG_DSN = os.environ.get("TEST_PG_DSN")

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not TEST_PG_DSN, reason="requiere PostgreSQL real (TEST_PG_DSN)"),
]

DIA = date(2026, 9, 15)


@contextlib.asynccontextmanager
async def _sesion(zona: str) -> AsyncIterator[AsyncSession]:
    assert TEST_PG_DSN is not None
    engine = create_async_engine(TEST_PG_DSN, poolclass=NullPool)
    try:
        async with engine.connect() as c:
            trans = await c.begin()
            try:
                await c.execute(text(f"SET LOCAL TIME ZONE '{zona}'"))
                yield AsyncSession(bind=c)
            finally:
                await trans.rollback()
    finally:
        await engine.dispose()


@pytest.mark.parametrize("zona", ["UTC", "America/Argentina/Buenos_Aires"])
async def test_el_dia_de_la_apertura_no_depende_de_la_zona_de_la_sesion(zona: str) -> None:
    async with _sesion(zona) as s:
        tid = uuid.uuid4()
        s.add(Tenant(tenant_id=tid, legal_name="T", display_name="T"))
        await s.flush()
        pid = uuid.uuid4()
        s.add(Product(id=pid, tenant_id=tid, name="P", sale_price_ars=Decimal("10"),
                      stock_units=0, provenance="REAL", custom_fields={}))
        await s.flush()
        # 00:30 del 15 en hora argentina, rotulada UTC (como guarda el ledger).
        s.add(InventoryMovement(
            tenant_id=tid, product_id=pid, movement_type="adjustment", qty=5,
            unit_cost=Decimal("6"), source_type=SOURCE_CATALOG_INITIAL_STOCK,
            source_event_id="import", occurred_at=datetime(2026, 9, 15, 0, 30, tzinfo=UTC),
        ))
        for hora in (datetime(2026, 9, 14, 23, 0), datetime(2026, 9, 15, 10, 0)):
            s.add(SaleEntry(
                tenant_id=tid, product_id=pid, amount=Decimal("10"), quantity=1,
                transaction_date=hora, payment_method="cash", cost_origin="sin_costo",
                cost_snapshot_version=1, cost_confirmed=False,
            ))
        await s.flush()

        ap = await aperturas_por_confirmar(s, tid)
        assert ap.sin_confirmar == 1
        assert [m.fecha_actual for m in ap.manuales] == [DIA]
        assert ap.ventas_sin_costo_antes == 1
        assert ap.ventas_sin_costo_con_fuente == 1
