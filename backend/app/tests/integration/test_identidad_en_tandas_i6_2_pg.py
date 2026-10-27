"""Las sentencias de identidad de operación van en tandas (I6-2), contra asyncpg.

El tope de 32.767 parámetros es de asyncpg: la suite SQLite lo mide en el
cursor (``services/test_identidad_en_tandas_i6_2.py``), acá se corre la misma
secuencia contra Postgres real, donde antes asyncpg rechazaba la sentencia::

    TEST_PG_DSN='postgresql+asyncpg://vektor:vektor@localhost:5432/vektor_pgtest' \\
        pytest app/tests/integration/test_identidad_en_tandas_i6_2_pg.py -m postgres -n 0 --no-cov
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncGenerator
from typing import cast

import pytest
import pytest_asyncio
from sqlalchemy import Table, delete, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from sqlalchemy.pool import NullPool

from app.persistence.db.base import Base
from app.persistence.db.engine_factory import crear_engine
from app.persistence.models.operation_identity import OperationIdentity, OperationIdentityLink
from app.persistence.models.tenant import Tenant
from app.tests.services.test_identidad_en_tandas_i6_2 import TOPE_DE_PARAMETROS, ejercitar_todo

TEST_PG_DSN = os.environ.get("TEST_PG_DSN")

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not TEST_PG_DSN, reason="requiere PostgreSQL real (TEST_PG_DSN)"),
]

_DDL_ADVISORY_KEY = 0x5645_4B54_4F52_1622  # "VEKTOR" + I6-2


@pytest_asyncio.fixture
async def pg_engine() -> AsyncGenerator[AsyncEngine, None]:
    assert TEST_PG_DSN is not None
    engine = crear_engine(TEST_PG_DSN, poolclass=NullPool)
    tablas = [
        cast("Table", m.__table__) for m in (Tenant, OperationIdentity, OperationIdentityLink)
    ]
    async with engine.begin() as conn:
        await conn.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _DDL_ADVISORY_KEY})
        await conn.run_sync(Base.metadata.create_all, tables=tablas, checkfirst=True)
    try:
        yield engine
    finally:
        await engine.dispose()


async def test_33000_claves_y_efectos_contra_asyncpg(pg_engine: AsyncEngine) -> None:
    factory = async_sessionmaker(pg_engine, expire_on_commit=False, autoflush=False)
    tid = uuid.uuid4()
    async with factory() as s:
        s.add(Tenant(tenant_id=tid, legal_name="T", display_name="T"))
        await s.commit()
    try:
        async with factory() as s:
            medidos = await ejercitar_todo(s, tid)
            await s.commit()
        assert max(medidos) <= TOPE_DE_PARAMETROS
    finally:
        async with factory() as s:
            await s.execute(
                delete(OperationIdentityLink).where(OperationIdentityLink.tenant_id == tid)
            )
            await s.execute(delete(OperationIdentity).where(OperationIdentity.tenant_id == tid))
            await s.execute(delete(Tenant).where(Tenant.tenant_id == tid))
            await s.commit()
