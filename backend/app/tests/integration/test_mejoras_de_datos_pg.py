"""«Mejoras de datos» contra Postgres real: lo que SQLite no ejercita.

SQLite no tiene advisory locks ni ``FOR UPDATE``: la garantía de que dos tareas
simultáneas sobre el mismo tenant no apliquen dos veces la misma mejora sólo se
prueba acá. Cada tarea usa su propia conexión (``NullPool``): los advisory locks
son por conexión.

    TEST_PG_DSN=... DATABASE_URL=... pytest -m postgres -n 0 \\
        app/tests/integration/test_mejoras_de_datos_pg.py --no-cov
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncGenerator, Iterator
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.jobs.mejoras_de_datos import procesar
from app.persistence.models.audit import DecisionAuditLog
from app.persistence.models.product import Product
from app.persistence.models.repair import DataRepairRun
from app.persistence.models.tenant import Tenant
from app.tests._mejora_juguete import VALOR, MejoraJuguete, registro_con

TEST_PG_DSN = os.environ.get("TEST_PG_DSN")

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.usefixtures("mock_score_trigger"),
    pytest.mark.skipif(not TEST_PG_DSN, reason="requiere PostgreSQL real (TEST_PG_DSN)"),
]


@pytest.fixture(autouse=True)
def _registro() -> Iterator[None]:
    with registro_con(MejoraJuguete()):
        yield


@pytest_asyncio.fixture(scope="module")
async def pg_engine() -> AsyncGenerator[AsyncEngine, None]:
    assert TEST_PG_DSN is not None
    engine = create_async_engine(TEST_PG_DSN, poolclass=NullPool)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def sm(
    pg_engine: AsyncEngine,
) -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    yield async_sessionmaker(pg_engine, expire_on_commit=False)


@pytest_asyncio.fixture
async def tenant_id(sm: async_sessionmaker[AsyncSession]) -> AsyncGenerator[uuid.UUID, None]:
    tid = uuid.uuid4()
    async with sm() as s:
        s.add(Tenant(tenant_id=tid, legal_name="T", display_name="T", status="ACTIVE"))
        await s.flush()
        for i in range(30):
            s.add(
                Product(
                    id=uuid.uuid4(),
                    tenant_id=tid,
                    name=f"P{i}",
                    sale_price_ars=Decimal("100"),
                    stock_units=0,
                )
            )
        await s.commit()
    try:
        yield tid
    finally:
        async with sm() as s:
            for modelo in (DecisionAuditLog, DataRepairRun, Product):
                await s.execute(delete(modelo).where(modelo.tenant_id == tid))
            await s.execute(delete(Tenant).where(Tenant.tenant_id == tid))
            await s.commit()


async def _cuenta(sm: async_sessionmaker[AsyncSession], modelo: Any, *cond: Any) -> int:
    async with sm() as s:
        return int(
            await s.scalar(select(func.count()).select_from(modelo).where(*cond)) or 0
        )


async def test_dos_tareas_simultaneas_aplican_una_sola_vez(
    sm: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> None:
    """Dos ``procesar`` a la vez sobre el mismo tenant: cada producto se completa y
    se audita UNA vez (no 2×30)."""
    r1, r2 = await asyncio.gather(procesar(sm, tenant_id), procesar(sm, tenant_id))

    assert r1["juguete"] + r2["juguete"] == 30
    assert (
        await _cuenta(
            sm,
            DecisionAuditLog,
            DecisionAuditLog.tenant_id == tenant_id,
            DecisionAuditLog.decision_type == "MEJORA_DE_DATOS:juguete",
        )
        == 30
    )
    assert (
        await _cuenta(
            sm, Product, Product.tenant_id == tenant_id, Product.description == VALOR
        )
        == 30
    )
    # Una tercera pasada ya no encuentra nada (el detector es el libro de cuentas).
    assert (await procesar(sm, tenant_id))["juguete"] == 0
