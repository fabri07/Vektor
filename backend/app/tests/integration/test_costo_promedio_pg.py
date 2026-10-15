"""M·P2a contra Postgres: el historial del costo promedio con la migración real.

SQLite no ve ``Numeric(18,6)`` ni los UUID nativos ni el ``FOR UPDATE`` del
estado; acá se reconstruye dos veces en transacciones reales con su commit.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncGenerator
from datetime import date, datetime
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.application.services import costo_promedio_service as svc
from app.application.services import stock_service
from app.persistence.models.inventory import InventoryBalance, InventoryMovement
from app.persistence.models.product import Product
from app.persistence.models.product_cost import ProductCostHistory, ProductCostState
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import SaleEntry
from app.tests.integration import _reread_scenario as esc

TEST_PG_DSN = os.environ.get("TEST_PG_DSN")

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not TEST_PG_DSN, reason="requiere TEST_PG_DSN (Postgres real)"),
]

_DDL_ADVISORY_KEY = 0x5645_4B54_4F52_4D50  # "VEKTOR" + MP


@pytest_asyncio.fixture(scope="module")
async def pg_engine() -> AsyncGenerator[AsyncEngine, None]:
    assert TEST_PG_DSN is not None
    engine = await esc.crear_engine(TEST_PG_DSN, _DDL_ADVISORY_KEY)
    try:
        yield engine
    finally:
        await engine.dispose()


async def _limpiar(sm: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID) -> None:
    async with sm() as s:
        for modelo in (
            ProductCostHistory, ProductCostState, InventoryMovement, InventoryBalance,
            SaleEntry, Product,
        ):
            await s.execute(delete(modelo).where(modelo.tenant_id == tenant_id))
        await s.execute(delete(Tenant).where(Tenant.tenant_id == tenant_id))
        await s.commit()


async def test_reconstruir_en_postgres(pg_engine: AsyncEngine) -> None:
    sm = async_sessionmaker(pg_engine, expire_on_commit=False)
    tid = uuid.uuid4()
    try:
        async with sm() as s:
            s.add(Tenant(tenant_id=tid, legal_name="MP", display_name="MP"))
            await s.flush()
            p = Product(tenant_id=tid, name="Vela", sale_price_ars=Decimal("300"), stock_units=0)
            s.add(p)
            await s.flush()
            pid = p.id
            await stock_service.increment_stock(
                pid, tid, 10, Decimal("100"), None, s, occurred_at=datetime(2026, 3, 1, 12)
            )
            venta = SaleEntry(
                tenant_id=tid, product_id=pid, amount=Decimal("1500"), quantity=5,
                transaction_date=datetime(2026, 3, 2, 12), payment_method="cash",
            )
            s.add(venta)
            await s.flush()
            await stock_service.decrement_stock(
                pid, tid, 5, stock_service.sale_source_event_id(venta.id), s,
                occurred_at=datetime(2026, 3, 2, 12),
            )
            await stock_service.increment_stock(
                pid, tid, 10, Decimal("200"), None, s, occurred_at=datetime(2026, 3, 3, 12)
            )
            await s.commit()

        for _ in range(2):
            async with sm() as s:
                r = await svc.reconstruir(s, tid, medir_brecha=True)
                await s.commit()
            assert (r.registros, r.aproximados, r.fuera_del_ledger) == (2, 0, 0)

        async with sm() as s:
            vigente = await svc.costo_a(s, tid, pid, date(2026, 3, 3))
            assert vigente is not None
            assert vigente.costo_promedio == Decimal("166.666667")
            estado = (
                await s.execute(select(ProductCostState).where(ProductCostState.product_id == pid))
            ).scalar_one()
            # La compra del armado marcó (1) y cada reconstrucción publicó (2 y 3).
            assert estado.revision == 3
            total = len(
                (
                    await s.execute(
                        select(ProductCostHistory).where(ProductCostHistory.tenant_id == tid)
                    )
                ).scalars().all()
            )
            assert total == 2
    finally:
        await _limpiar(sm, tid)


async def test_costos_a_lote_en_postgres_lee_lo_mismo_que_costo_a(pg_engine: AsyncEngine) -> None:
    """M·P4-1: el ``row_number()`` del lote en Postgres (UUID nativo, ``Numeric(18,6)``)
    elige el mismo registro que ``costo_a`` para cada producto."""
    sm = async_sessionmaker(pg_engine, expire_on_commit=False)
    tid = uuid.uuid4()
    try:
        async with sm() as s:
            s.add(Tenant(tenant_id=tid, legal_name="MP4", display_name="MP4"))
            await s.flush()
            ids: list[uuid.UUID] = []
            for nombre, costos in (("A", ("100", "200")), ("B", ("50",)), ("C", ())):
                p = Product(
                    tenant_id=tid, name=nombre, sale_price_ars=Decimal("300"), stock_units=0
                )
                s.add(p)
                await s.flush()
                ids.append(p.id)
                for n, costo in enumerate(costos, start=1):
                    await stock_service.increment_stock(
                        p.id, tid, 10, Decimal(costo), None, s,
                        occurred_at=datetime(2026, 3, n, 12),
                    )
            await svc.reconstruir(s, tid)
            await s.commit()

        async with sm() as s:
            for dia in (date(2026, 2, 28), date(2026, 3, 1), date(2026, 3, 20)):
                lote = await svc.costos_a_lote(s, tid, ids, dia)
                for pid in ids:
                    uno = await svc.costo_a(s, tid, pid, dia)
                    assert (lote[pid].id if pid in lote else None) == (uno.id if uno else None)
            final = await svc.costos_a_lote(s, tid, ids, date(2026, 3, 20))
            assert ids[2] not in final
            assert final[ids[0]].costo_promedio == Decimal("150")
    finally:
        await _limpiar(sm, tid)
