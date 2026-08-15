"""F-ID: contención real de `record_identifier` contra PostgreSQL.

Mismo motivo que `test_entity_code_sequences_contention_pg.py` — sin conexiones
concurrentes reales, un SELECT-then-INSERT sin guard "pasaría" en SQLite aunque
tenga una ventana TOCTOU real. Acá la ventana es la que el code review marcó:
dos llamadas concurrentes que no se ven entre sí en su SELECT insertan ambas, y
sin `guarded_savepoint` la segunda revienta con un `IntegrityError` crudo que
aborta TODA la transacción en PostgreSQL, no solo la operación.

Gating: se skippea limpio sin `TEST_PG_DSN`. Mismo patrón que
`test_entity_code_sequences_contention_pg.py`.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from sqlalchemy import delete, select, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.inspection import inspect as sa_inspect
from sqlalchemy.pool import NullPool

from app.application.services.entity_code_service import (
    EntityIdentifierConflictError,
    record_identifier,
)
from app.persistence.models.entity_identifier import EntityIdentifier
from app.persistence.models.tenant import Tenant

TEST_PG_DSN = os.environ.get("TEST_PG_DSN")

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not TEST_PG_DSN, reason="requiere PostgreSQL real (TEST_PG_DSN)"),
]

# 8 bytes exactos: "VEKTOR" + "EID1" (EntityIDentifier).
_DDL_ADVISORY_KEY = 0x5645_4B54_4F52_E1D1


def _table_names(sync_conn: Connection) -> list[str]:
    return sa_inspect(sync_conn).get_table_names()


@pytest_asyncio.fixture(scope="module")
async def pg_engine() -> AsyncGenerator[AsyncEngine, None]:
    assert TEST_PG_DSN is not None
    engine = create_async_engine(TEST_PG_DSN, poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _DDL_ADVISORY_KEY})
        tables = await conn.run_sync(_table_names)
        if "entity_identifiers" not in tables or "tenants" not in tables:
            pytest.skip("schema sin migrar (falta entity_identifiers): correr alembic")
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
                delete(EntityIdentifier).where(EntityIdentifier.tenant_id == tenant_id)
            )
            await s.execute(delete(Tenant).where(Tenant.tenant_id == tenant_id))
            await s.commit()


async def _record_and_commit(
    sm: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID, entity_id: uuid.UUID
) -> uuid.UUID:
    async with sm() as s:
        row = await record_identifier(
            s,
            tenant_id,
            "customer",
            entity_id,
            "business_code",
            "business",
            "ERP-0042",
            "business",
        )
        await s.commit()
        return row.id


async def test_llamadas_concurrentes_de_la_misma_entidad_no_revientan_la_transaccion(
    sessionmaker: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> None:
    """12 sesiones distintas registran el MISMO identificador para la MISMA
    entidad al mismo tiempo — ninguna SELECT ve el insert de las demás antes
    de intentar el suyo. Sin `guarded_savepoint`, todas menos la primera
    revientan con un `IntegrityError` crudo (la transacción externa abortada,
    ver `_savepoint.py`). Con el guard, las perdedoras se recuperan re-consultando
    y actualizando la fila que ganó la carrera — ninguna excepción, una sola fila.
    """
    entity_id = uuid.uuid4()
    n = 12
    results = await asyncio.gather(
        *(_record_and_commit(sessionmaker, tenant_id, entity_id) for _ in range(n)),
        return_exceptions=True,
    )

    failures = [r for r in results if isinstance(r, BaseException)]
    assert not failures, f"llamadas concurrentes no debían levantar excepción: {failures}"
    # Todas resuelven a la MISMA fila (idempotencia): ganó una, el resto la reusó.
    assert len(set(results)) == 1

    async with sessionmaker() as s:
        rows = (
            await s.execute(
                select(EntityIdentifier).where(
                    EntityIdentifier.tenant_id == tenant_id,
                    EntityIdentifier.entity_type == "customer",
                    EntityIdentifier.identifier_type == "business_code",
                    EntityIdentifier.namespace == "business",
                    EntityIdentifier.revoked_at.is_(None),
                )
            )
        ).scalars().all()
        assert len(rows) == 1
        assert rows[0].entity_id == entity_id


async def test_llamadas_concurrentes_de_entidades_distintas_dejan_una_sola_ganadora(
    sessionmaker: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> None:
    """F-I(A): dos filas de DOS archivos distintos importándose al mismo
    tiempo, cada una aprendiendo el MISMO `business_code` para una entidad
    DISTINTA — exactamente la carrera que motiva la degradación a
    `unresolved` en `_record_row_business_code`. Una gana, la otra recibe
    `EntityIdentifierConflictError` (nunca gana el primero en silencio ni
    se corrompe la transacción de la que pierde)."""
    entity_a = uuid.uuid4()
    entity_b = uuid.uuid4()
    results = await asyncio.gather(
        _record_and_commit(sessionmaker, tenant_id, entity_a),
        _record_and_commit(sessionmaker, tenant_id, entity_b),
        return_exceptions=True,
    )

    successes = [r for r in results if not isinstance(r, BaseException)]
    conflicts = [r for r in results if isinstance(r, EntityIdentifierConflictError)]
    other_failures = [
        r
        for r in results
        if isinstance(r, BaseException) and not isinstance(r, EntityIdentifierConflictError)
    ]
    assert not other_failures, f"solo se espera EntityIdentifierConflictError o éxito: {results}"
    assert len(successes) == 1
    assert len(conflicts) == 1

    async with sessionmaker() as s:
        rows = (
            await s.execute(
                select(EntityIdentifier).where(
                    EntityIdentifier.tenant_id == tenant_id,
                    EntityIdentifier.entity_type == "customer",
                    EntityIdentifier.identifier_type == "business_code",
                    EntityIdentifier.namespace == "business",
                    EntityIdentifier.revoked_at.is_(None),
                )
            )
        ).scalars().all()
        assert len(rows) == 1
        assert rows[0].entity_id in (entity_a, entity_b)
