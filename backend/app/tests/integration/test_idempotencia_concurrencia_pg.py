"""El candado de la idempotencia es el UNIQUE, no un SELECT previo — contra PostgreSQL.

Por qué no alcanza SQLite
-------------------------
Lo que se afirma acá es que dos peticiones SIMULTÁNEAS con la misma
``Idempotency-Key`` no pueden reclamar las dos. Eso exige dos conexiones físicas
compitiendo contra el mismo índice: en SQLite no hay tal cosa, la suite pasa y no
prueba nada. Ver ``[[feedback_sqlite_masks_postgres]]``.

Es la propiedad que importa para una caja. El escenario real no es un reintento
ordenado sino el cliente que reenvía porque no recibió respuesta **mientras la
primera petición sigue en vuelo**: si las dos reclamaran, se cobra dos veces.

Gating: se skippea limpio sin ``TEST_PG_DSN``. Para correrlo::

    TEST_PG_DSN='postgresql+asyncpg://vektor:vektor@localhost:5432/vektor_pgtest' \\
        pytest app/tests/integration/test_idempotencia_concurrencia_pg.py -v --no-cov

El schema lo provee ``alembic upgrade head``. Si falta la tabla, el módulo se
skippea; si falta el UNIQUE, **falla**, porque sin él no habría colisión y los
tests pasarían vacíos.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncGenerator
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.inspection import inspect as sa_inspect
from sqlalchemy.pool import NullPool

from app.application.services.idempotency import (
    ClaveReusada,
    Reclamada,
    Repeticion,
    claim_idempotent_request,
    record_idempotent_response,
)
from app.persistence.models.idempotency import IdempotencyRecord
from app.persistence.models.tenant import Tenant

TEST_PG_DSN = os.environ.get("TEST_PG_DSN")

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not TEST_PG_DSN, reason="requiere PostgreSQL real (TEST_PG_DSN)"),
]

_TABLA = "idempotency_records"
_UNIQUE = "uq_idempotency_records_tenant_key"

_PAYLOAD = {"items": [{"product_id": "p1", "quantity": 2, "unit_price": "1000.00"}]}


def _table_names(sync_conn: Connection) -> list[str]:
    return sa_inspect(sync_conn).get_table_names()


def _indexes(sync_conn: Connection) -> list[Any]:
    insp = sa_inspect(sync_conn)
    return [*insp.get_indexes(_TABLA), *insp.get_unique_constraints(_TABLA)]


@pytest_asyncio.fixture(scope="module")
async def pg_engine() -> AsyncGenerator[AsyncEngine, None]:
    """NullPool: sin él las dos sesiones comparten conexión y no hay contención real."""
    assert TEST_PG_DSN is not None
    engine = create_async_engine(TEST_PG_DSN, poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.execute(text("SELECT 1"))
        if _TABLA not in await conn.run_sync(_table_names):
            pytest.skip(f"schema sin migrar (falta {_TABLA}): correr alembic upgrade head")
        nombres = {ix.get("name") for ix in await conn.run_sync(_indexes)}
        assert _UNIQUE in nombres, f"falta {_UNIQUE}: el test no probaría la contención"
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
                delete(IdempotencyRecord).where(IdempotencyRecord.tenant_id == tenant_id)
            )
            await s.execute(delete(Tenant).where(Tenant.tenant_id == tenant_id))
            await s.commit()


async def test_dos_peticiones_simultaneas_solo_una_reclama(
    sessionmaker: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> None:
    """El caso que produce el doble cobro: el reenvío llega con la primera en vuelo."""

    async def intentar() -> Reclamada | Repeticion | ClaveReusada:
        async with sessionmaker() as s:
            resultado = await claim_idempotent_request(
                s, tenant_id, "k-concurrente", "IDEMPOTENT_POST_SALE_BATCH", _PAYLOAD
            )
            if isinstance(resultado, Reclamada):
                await record_idempotent_response(
                    s, tenant_id, "k-concurrente", {"sale_group_id": "g1"}, 201
                )
            await s.commit()
            return resultado

    a, b = await asyncio.gather(intentar(), intentar())

    reclamos = [r for r in (a, b) if isinstance(r, Reclamada)]
    assert len(reclamos) == 1, f"reclamaron {len(reclamos)}: se cobraría dos veces"

    async with sessionmaker() as s:
        filas = await s.scalar(
            select(func.count(IdempotencyRecord.id)).where(
                IdempotencyRecord.tenant_id == tenant_id
            )
        )
    assert filas == 1


async def test_la_perdedora_puede_recuperar_el_resultado(
    sessionmaker: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> None:
    """Perder la carrera no es un error: es de donde sale el ticket para reimprimir."""
    async with sessionmaker() as s:
        assert isinstance(
            await claim_idempotent_request(s, tenant_id, "k-rec", "ACC", _PAYLOAD), Reclamada
        )
        await record_idempotent_response(s, tenant_id, "k-rec", {"sale_group_id": "g9"}, 201)
        await s.commit()

    async with sessionmaker() as s:
        repeticion = await claim_idempotent_request(s, tenant_id, "k-rec", "ACC", _PAYLOAD)
    assert isinstance(repeticion, Repeticion)
    assert repeticion.respuesta == {"sale_group_id": "g9"}
    assert repeticion.http_status == 201


async def test_la_sesion_perdedora_queda_usable(
    sessionmaker: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> None:
    """En PostgreSQL un IntegrityError fuera del savepoint aborta la transacción entera.

    Si el claim no estuviera envuelto en ``guarded_savepoint``, el SELECT que
    recupera el original reventaría con ``InFailedSQLTransaction`` y el reintento
    devolvería un 500 en vez del ticket.
    """
    async with sessionmaker() as s:
        await claim_idempotent_request(s, tenant_id, "k-usable", "ACC", _PAYLOAD)
        await record_idempotent_response(s, tenant_id, "k-usable", {"ok": True}, 201)
        await s.commit()

    async with sessionmaker() as s:
        resultado = await claim_idempotent_request(s, tenant_id, "k-usable", "ACC", _PAYLOAD)
        assert isinstance(resultado, Repeticion)
        # La sesión sigue viva después de la colisión: puede leer y escribir.
        total = await s.scalar(
            select(func.count(IdempotencyRecord.id)).where(
                IdempotencyRecord.tenant_id == tenant_id
            )
        )
        assert total == 1
        await claim_idempotent_request(s, tenant_id, "k-otra", "ACC", _PAYLOAD)
        await s.commit()
