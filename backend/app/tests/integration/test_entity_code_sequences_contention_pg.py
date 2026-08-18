"""F-ID: contención real de `assign_next_sequence` contra PostgreSQL.

Por qué no alcanza SQLite: no hay conexiones concurrentes reales compitiendo
por la misma fila, así que un ``UPDATE ... RETURNING`` sin lock de fila
observable "pasaría" igual aunque la implementación tuviera una ventana
TOCTOU. Lo que se afirma acá, que SQLite no puede afirmar: N sesiones
concurrentes pidiendo secuencia para el MISMO ``(tenant, entity_type,
prefix)`` nunca reciben el mismo valor — ni siquiera en la primera llamada,
donde hay una carrera real por crear la fila de secuencia.

Gating: se skippea limpio sin ``TEST_PG_DSN``. Mismo patrón que
``test_savepoint_contention_pg.py`` — ver ese módulo para el razonamiento
completo del setup (NullPool, advisory lock de DDL, schema vía Alembic).
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncGenerator
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import delete, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.inspection import inspect as sa_inspect
from sqlalchemy.pool import NullPool

from app.application.services.entity_code_service import assign_next_sequence
from app.persistence.models.entity_code_sequence import EntityCodeSequence
from app.persistence.models.tenant import Tenant

TEST_PG_DSN = os.environ.get("TEST_PG_DSN")

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not TEST_PG_DSN, reason="requiere PostgreSQL real (TEST_PG_DSN)"),
]

# Clave arbitraria y estable para serializar el CREATE entre workers xdist.
# 8 bytes exactos (bigint de Postgres es int64): "VEKTOR" + "F1D0" (F-ID).
_DDL_ADVISORY_KEY = 0x5645_4B54_4F52_F1D0

_SEQUENCE_UNIQUE = "uq_entity_code_sequences_tenant_type_prefix"


def _table_names(sync_conn: Connection) -> list[str]:
    return sa_inspect(sync_conn).get_table_names()


def _sequence_indexes(sync_conn: Connection) -> list[Any]:
    return sa_inspect(sync_conn).get_unique_constraints("entity_code_sequences") + [
        {"name": ix["name"]} for ix in sa_inspect(sync_conn).get_indexes("entity_code_sequences")
    ]


@pytest_asyncio.fixture(scope="module")
async def pg_engine() -> AsyncGenerator[AsyncEngine, None]:
    assert TEST_PG_DSN is not None
    engine = create_async_engine(TEST_PG_DSN, poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _DDL_ADVISORY_KEY})
        tables = await conn.run_sync(_table_names)
        if "entity_code_sequences" not in tables or "tenants" not in tables:
            pytest.skip("schema sin migrar (falta entity_code_sequences): correr alembic")
        constraints = await conn.run_sync(_sequence_indexes)
        names = {c["name"] for c in constraints}
        assert _SEQUENCE_UNIQUE in names, (
            f"falta {_SEQUENCE_UNIQUE}: el test no probaría la contención real"
        )
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def tenant_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest_asyncio.fixture
async def sessionmaker(
    pg_engine: AsyncEngine, tenant_id: uuid.UUID
) -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    sm = async_sessionmaker(pg_engine, expire_on_commit=False)
    async with sm() as s:
        s.add(Tenant(tenant_id=tenant_id, legal_name="T", display_name="T"))
        await s.commit()
    try:
        yield sm
    finally:
        async with sm() as s:
            await s.execute(
                delete(EntityCodeSequence).where(EntityCodeSequence.tenant_id == tenant_id)
            )
            await s.execute(delete(Tenant).where(Tenant.tenant_id == tenant_id))
            await s.commit()


async def _assign_and_commit(
    sm: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> int:
    async with sm() as s:
        value = await assign_next_sequence(s, tenant_id, "product", "TEX")
        await s.commit()
        return value


async def test_n_llamadas_concurrentes_nunca_repiten_valor(
    sessionmaker: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> None:
    """La carrera real: 12 sesiones distintas piden secuencia para el mismo
    prefijo AL MISMO TIEMPO, incluyendo la primera llamada (donde la fila de
    secuencia todavía no existe y hay que crearla)."""
    n = 12
    results = await asyncio.gather(
        *(_assign_and_commit(sessionmaker, tenant_id) for _ in range(n))
    )
    assert sorted(results) == list(range(1, n + 1)), (
        f"se esperaban {n} valores únicos y consecutivos, se obtuvo {sorted(results)}"
    )
