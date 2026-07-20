"""Contención real de los get-or-create sobre SAVEPOINT (F5-A), contra PostgreSQL.

Por qué no alcanza SQLite
-------------------------
El defecto que corrige ``_savepoint`` sólo se manifiesta con concurrencia real: si
el ``add`` ocurre ANTES del ``begin_nested()``, el INSERT se emite en la transacción
EXTERNA (``_take_snapshot`` flushea incondicionalmente al crear el savepoint), y en
PostgreSQL el ``IntegrityError`` **aborta la transacción entera** — el re-query del
``except`` revienta con ``InFailedSQLTransaction``. En SQLite no hay dos sesiones
compitiendo y el fallo es benigno, así que una suite verde no prueba nada de esto.
Ver ``[[feedback_sqlite_masks_postgres]]``.

Lo que se afirma acá, y que SQLite no puede afirmar:

1. Dos sesiones concurrentes resuelven al MISMO sentinela (una crea, otra re-queryea).
2. La sesión PERDEDORA queda **utilizable**: puede leer Y escribir después de la
   colisión. Con el orden invertido esto fallaba.
3. Una violación AJENA (FK inexistente, NOT NULL) se **propaga** en vez de leerse
   como "ya existía".

Gating: se **skippea limpio** sin ``TEST_PG_DSN``. Para correrlo::

    TEST_PG_DSN='postgresql+asyncpg://vektor:vektor@localhost:5432/vektor' \\
        pytest app/tests/integration/test_savepoint_contention_pg.py -v --no-cov

El schema lo provee ``alembic upgrade head`` (paso 6b de ci-backend.yml, antes de los
tests ``postgres``); localmente hay que correrlo a mano. NO se usa ``create_all``:
``customers`` tiene server_defaults que sólo son válidos vía Alembic. Si falta el
schema el módulo se skippea; si falta el índice del sentinela **falla**, porque sin él
no habría colisión y los tests pasarían vacíos.

Aislamiento bajo xdist: cada test usa un ``tenant_id`` único y la limpieza borra SOLO
esas filas (nunca ``TRUNCATE`` global).
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncGenerator
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import delete, select, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.inspection import inspect as sa_inspect
from sqlalchemy.pool import NullPool

from app.application.services.customer_sentinel import (
    LOCAL_CUSTOMER_NAME,
    resolve_or_create_local_sentinel,
)
from app.application.services.idempotency import claim_idempotency_key
from app.persistence.models._sentinel import SENTINEL_FLAG_KEY
from app.persistence.models.customer import Customer
from app.persistence.models.memory import OperationFingerprint
from app.persistence.models.tenant import Tenant

TEST_PG_DSN = os.environ.get("TEST_PG_DSN")

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not TEST_PG_DSN, reason="requiere PostgreSQL real (TEST_PG_DSN)"),
]

# Clave arbitraria y estable para serializar el CREATE entre workers xdist.
_DDL_ADVISORY_KEY = 0x5645_4B54_4F52_0F50  # "VEKTOR" + F5-A

_SENTINEL_INDEX = "uq_customers_sentinel_per_tenant"


def _table_names(sync_conn: Connection) -> list[str]:
    return sa_inspect(sync_conn).get_table_names()


def _customer_indexes(sync_conn: Connection) -> list[Any]:
    return sa_inspect(sync_conn).get_indexes("customers")


@pytest_asyncio.fixture(scope="module")
async def pg_engine() -> AsyncGenerator[AsyncEngine, None]:
    """Engine asyncpg propio (NullPool → cada sesión, su conexión física).

    Sin ``NullPool`` las dos sesiones compartirían conexión y no habría contención
    real: el test pasaría sin probar nada.
    """
    assert TEST_PG_DSN is not None
    engine = create_async_engine(TEST_PG_DSN, poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _DDL_ADVISORY_KEY})
        # NO se usa `create_all`: `customers` tiene server_defaults que sólo son
        # válidos vía Alembic (en PostgreSQL, `create_all` falla con "invalid input
        # syntax for type json"). El schema lo crea `alembic upgrade head` —el paso 6b
        # de ci-backend.yml, que corre ANTES de los tests `postgres`—. Localmente:
        #   DATABASE_URL=postgresql://... alembic upgrade head
        tables = await conn.run_sync(_table_names)
        missing = [t for t in ("customers", "operation_fingerprints", "tenants") if t not in tables]
        if missing:
            pytest.skip(f"schema sin migrar (faltan {', '.join(missing)}): correr alembic")
        # El índice parcial del sentinela es LA garantía que se está probando: sin él
        # no hay colisión y el test pasaría vacío. Se verifica, no se asume.
        indexes = await conn.run_sync(_customer_indexes)
        assert _SENTINEL_INDEX in {
            ix["name"] for ix in indexes
        }, f"falta {_SENTINEL_INDEX}: el test no probaría la contención"
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
                delete(OperationFingerprint).where(OperationFingerprint.tenant_id == tenant_id)
            )
            await s.execute(delete(Customer).where(Customer.tenant_id == tenant_id))
            await s.execute(delete(Tenant).where(Tenant.tenant_id == tenant_id))
            await s.commit()


async def test_sentinela_concurrente_resuelve_al_mismo_id_y_el_perdedor_sigue_usable(
    sessionmaker: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> None:
    """El caso que motiva ``_savepoint``, sin sleeps ni races de timing.

    El holder inserta el sentinela y NO commitea → el competidor BLOQUEA en el índice
    único (PostgreSQL retiene el segundo INSERT hasta que el primero resuelve). Tras
    el commit del holder, el competidor recibe la violación, re-queryea y devuelve el
    MISMO id. Con el orden invertido (``add`` antes del savepoint) su transacción
    quedaría abortada y el re-query fallaría con ``InFailedSQLTransaction``.
    """
    async with sessionmaker() as holder, sessionmaker() as competidor:
        holder_id = await resolve_or_create_local_sentinel(holder, tenant_id)

        task = asyncio.create_task(resolve_or_create_local_sentinel(competidor, tenant_id))
        # Debe estar BLOQUEADO mientras el holder no commitea (aserción determinística).
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(task), timeout=1.0)

        await holder.commit()
        competidor_id = await asyncio.wait_for(task, timeout=10.0)

        assert competidor_id == holder_id, "cada sesión resolvió a un sentinela distinto"

        # La transacción PERDEDORA sigue viva: lee...
        found = (
            (
                await competidor.execute(
                    select(Customer.id).where(
                        Customer.tenant_id == tenant_id,
                        Customer.custom_fields[SENTINEL_FLAG_KEY].as_string() == "true",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert found == [holder_id]
        # ...y ESCRIBE (lo que fallaría con la transacción abortada).
        competidor.add(Customer(tenant_id=tenant_id, name="Cliente posterior", custom_fields={}))
        await competidor.commit()

    async with sessionmaker() as verify:
        nombres = (
            (
                await verify.execute(
                    select(Customer.name)
                    .where(Customer.tenant_id == tenant_id)
                    .order_by(Customer.name)
                )
            )
            .scalars()
            .all()
        )
    assert nombres == ["Cliente posterior", LOCAL_CUSTOMER_NAME]


async def test_violacion_ajena_se_propaga_y_no_se_lee_como_duplicado(
    sessionmaker: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> None:
    """Una FK inexistente NO puede convertirse en "ya existía" (clasificador).

    En PostgreSQL las FKs sí se imponen (en SQLite no, sin ``PRAGMA foreign_keys``),
    así que este contrato sólo es verificable acá. ``claim_idempotency_key`` devolvería
    ``False`` —"replay", el caller responde 409— si tragara cualquier IntegrityError.
    """
    inexistente = uuid.uuid4()
    async with sessionmaker() as session:
        with pytest.raises(IntegrityError):
            await claim_idempotency_key(session, inexistente, "k-huerfana", "TEST")


async def test_claim_idempotencia_concurrente_gana_exactamente_uno(
    sessionmaker: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> None:
    """Dos requests con la misma Idempotency-Key: una reclama, la otra ve el replay."""

    async def _claim() -> bool:
        async with sessionmaker() as session:
            claimed = await claim_idempotency_key(session, tenant_id, "k-compartida", "TEST")
            await session.commit()
            return claimed

    primero, segundo = await asyncio.gather(_claim(), _claim())
    assert [primero, segundo].count(True) == 1
