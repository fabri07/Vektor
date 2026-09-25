"""Una hora con zona se guarda en hora argentina — contra PostgreSQL (B5, [R3]).

`transaction_date` es `timestamp without time zone`. Antes del arreglo, un
`...T02:00:00Z` pasaba el validador con su zona y, en PostgreSQL, **el INSERT
reventaba**: asyncpg rechaza un datetime con zona en una columna sin zona
(`DataError: can't subtract offset-naive and offset-aware datetimes`) → 500.
Medido revirtiendo el arreglo. En SQLite, en cambio, se guardaba la hora UTC
como si fuera local — por eso la suite de SQLite no lo veía como un error.

Acá se recorre el camino real: schema → valor normalizado → persistido →
releído con `func.date`, que es lo que usan los agregados por día.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncGenerator
from datetime import timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.domain.business_time import today_ar
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import SaleEntry
from app.schemas.transaction import CreateSaleRequest

TEST_PG_DSN = os.environ.get("TEST_PG_DSN")

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not TEST_PG_DSN, reason="requiere PostgreSQL real (TEST_PG_DSN)"),
]


@pytest_asyncio.fixture
async def sesiones() -> AsyncGenerator[tuple[async_sessionmaker[AsyncSession], uuid.UUID], None]:
    assert TEST_PG_DSN is not None
    engine = create_async_engine(TEST_PG_DSN, poolclass=NullPool)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    tenant_id = uuid.uuid4()
    async with sm() as s:
        s.add(Tenant(tenant_id=tenant_id, legal_name="T", display_name="T"))
        await s.commit()
    try:
        yield sm, tenant_id
    finally:
        async with sm() as s:
            await s.execute(delete(SaleEntry).where(SaleEntry.tenant_id == tenant_id))
            await s.execute(delete(Tenant).where(Tenant.tenant_id == tenant_id))
            await s.commit()
        await engine.dispose()


async def test_la_venta_de_las_2300_argentinas_cae_en_su_dia(
    sesiones: tuple[async_sessionmaker[AsyncSession], uuid.UUID],
) -> None:
    sm, tenant_id = sesiones
    hoy = today_ar()
    pedido = CreateSaleRequest.model_validate(
        {"amount": "100.00", "payment_method": "cash",
         "transaction_date": f"{hoy.isoformat()}T02:00:00Z"}
    )
    async with sm() as s:
        venta = SaleEntry(
            tenant_id=tenant_id,
            amount=Decimal("100.00"),
            quantity=1,
            transaction_date=pedido.transaction_date,
            payment_method="cash",
        )
        s.add(venta)
        await s.commit()
        venta_id = venta.id

    ayer = hoy - timedelta(days=1)
    async with sm() as s:
        guardada, dia = (
            await s.execute(
                select(SaleEntry.transaction_date, func.date(SaleEntry.transaction_date)).where(
                    SaleEntry.id == venta_id
                )
            )
        ).one()
    assert guardada.tzinfo is None
    assert (guardada.hour, guardada.minute) == (23, 0)
    assert dia == ayer
