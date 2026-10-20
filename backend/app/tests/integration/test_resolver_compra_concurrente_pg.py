"""B·5b: dos sesiones resuelven la MISMA fila de «Otros» a la vez — contra Postgres.

La fila se toma con ``FOR UPDATE`` y tiene que estar PENDING: la segunda espera
el bloqueo, reevalúa y encuentra la fila ya IMPORTED. Resultado: una sola compra
(un gasto y un movimiento por línea) y un 409. En SQLite ``FOR UPDATE`` no
existe y la suite pasaría sin probar nada.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncGenerator
from decimal import Decimal

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.api.v1 import others
from app.domain.business_time import today_ar
from app.persistence.models.audit import DecisionAuditLog
from app.persistence.models.inventory import InventoryBalance, InventoryMovement
from app.persistence.models.product import Product
from app.persistence.models.product_cost import ProductCostHistory, ProductCostState
from app.persistence.models.supplier import Supplier
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import ExpenseEntry
from app.persistence.models.unclassified_record import UnclassifiedRecord
from app.persistence.models.user import User
from app.tests.integration import _reread_scenario as esc

TEST_PG_DSN = os.environ.get("TEST_PG_DSN")

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not TEST_PG_DSN, reason="requiere PostgreSQL real (TEST_PG_DSN)"),
]

_DDL_ADVISORY_KEY = 0x5645_4B54_4F52_4235  # "VEKTORB5"


@pytest_asyncio.fixture(scope="module")
async def pg_engine() -> AsyncGenerator[AsyncEngine, None]:
    assert TEST_PG_DSN is not None
    engine = await esc.crear_engine(TEST_PG_DSN, _DDL_ADVISORY_KEY)
    try:
        yield engine
    finally:
        await engine.dispose()


_Escenario = tuple[async_sessionmaker[AsyncSession], uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]


@pytest_asyncio.fixture
async def escenario(pg_engine: AsyncEngine) -> AsyncGenerator[_Escenario, None]:
    fabrica = async_sessionmaker(pg_engine, expire_on_commit=False)
    tid, uid = uuid.uuid4(), uuid.uuid4()
    async with fabrica() as s:
        s.add(Tenant(tenant_id=tid, legal_name="B5b", display_name="B5b"))
        await s.flush()
        s.add(
            User(
                user_id=uid,
                tenant_id=tid,
                email=f"{uid}@b5b.test",
                full_name="Dueña",
                password_hash="x",
                role_code="OWNER",
                is_active=True,
            )
        )
        p = Product(tenant_id=tid, name="Vela", sale_price_ars=Decimal("300"), stock_units=0)
        s.add(p)
        rec = UnclassifiedRecord(
            tenant_id=tid,
            source="ingestion",
            context_label="Compra sin artículos",
            row_data={"monto": "1000"},
            suggested_entity="expense",
            status="PENDING",
        )
        s.add(rec)
        await s.commit()
        pid, rid = p.id, rec.id
    try:
        yield fabrica, tid, uid, pid, rid
    finally:
        async with fabrica() as s:
            for modelo in (
                DecisionAuditLog, ExpenseEntry, ProductCostHistory, ProductCostState,
                InventoryMovement, InventoryBalance, UnclassifiedRecord, Product, Supplier, User,
            ):
                await s.execute(delete(modelo).where(modelo.tenant_id == tid))
            await s.execute(delete(Tenant).where(Tenant.tenant_id == tid))
            await s.commit()


async def test_dos_resoluciones_simultaneas_dan_una_compra_y_un_409(
    escenario: _Escenario,
) -> None:
    fabrica, tid, uid, pid, rid = escenario
    body = others.ResolvePurchaseRequest(
        transaction_date=f"{today_ar().isoformat()}T10:00:00",  # type: ignore[arg-type]
        lines=[others.ResolvePurchaseLine(product_id=pid, quantity=4, amount=Decimal("1000.00"))],
    )

    async def resolver() -> str:
        async with fabrica() as s:
            tenant = await s.get(Tenant, tid)
            user = await s.get(User, uid)
            assert tenant is not None and user is not None
            try:
                await others.resolve_purchase(
                    rid, body, tenant=tenant, user=user, _maintenance_guard=None, session=s
                )
            except HTTPException as exc:
                await s.rollback()
                return f"{exc.status_code}"
            await s.commit()
            return "200"

    resultados = sorted(await asyncio.gather(resolver(), resolver()))
    assert resultados == ["200", "409"], resultados

    async with fabrica() as s:
        gastos = await s.scalar(
            select(func.count(ExpenseEntry.id)).where(ExpenseEntry.tenant_id == tid)
        )
        movs = await s.scalar(
            select(func.count(InventoryMovement.id)).where(InventoryMovement.tenant_id == tid)
        )
        producto = await s.get(Product, pid)
    assert (gastos, movs) == (1, 1)
    assert producto is not None and producto.stock_units == 4
