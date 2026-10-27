"""La huella del chat con vencimiento (X4-1), contra PostgreSQL real.

`claim_operation_fingerprint_con_ventana` borra la huella vencida y recién ahí
reclama. Con dos sesiones a la vez, después del vencimiento, las dos borran la
misma fila: lo que se afirma acá es que igual gana UNA sola (la otra espera el
lock de la fila borrada, encuentra la nueva en el unique y contesta duplicado) y
que la perdedora sigue usable. En SQLite no hay dos sesiones compitiendo, así que
la suite común no puede probarlo.

Gating: se skippea sin ``TEST_PG_DSN``; el schema lo provee ``alembic upgrade head``.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta

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

from app.application.services.idempotency import claim_operation_fingerprint_con_ventana
from app.persistence.models.memory import OperationFingerprint
from app.persistence.models.tenant import Tenant

TEST_PG_DSN = os.environ.get("TEST_PG_DSN")

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not TEST_PG_DSN, reason="requiere PostgreSQL real (TEST_PG_DSN)"),
]

_HUELLA = "chat:x4-carrera"
_VENTANA = timedelta(minutes=5)


def _table_names(sync_conn: Connection) -> list[str]:
    return sa_inspect(sync_conn).get_table_names()


@pytest_asyncio.fixture(scope="module")
async def pg_engine() -> AsyncGenerator[AsyncEngine, None]:
    """NullPool: cada sesión, su conexión física (si no, no hay contención real)."""
    assert TEST_PG_DSN is not None
    engine = create_async_engine(TEST_PG_DSN, poolclass=NullPool)
    async with engine.begin() as conn:
        tablas = await conn.run_sync(_table_names)
        if "operation_fingerprints" not in tablas or "tenants" not in tablas:
            pytest.skip("schema sin migrar: correr alembic")
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
    """Un negocio con la huella YA VENCIDA (de ayer)."""
    sm = async_sessionmaker(pg_engine, expire_on_commit=False)
    async with sm() as s:
        s.add(Tenant(tenant_id=tenant_id, legal_name="T", display_name="T"))
        await s.flush()
        s.add(
            OperationFingerprint(
                tenant_id=tenant_id,
                fingerprint=_HUELLA,
                action_type="REGISTER_SALE",
                executed_at=datetime.now(UTC) - timedelta(days=1),
            )
        )
        await s.commit()
    try:
        yield sm
    finally:
        async with sm() as s:
            await s.execute(
                delete(OperationFingerprint).where(OperationFingerprint.tenant_id == tenant_id)
            )
            await s.execute(delete(Tenant).where(Tenant.tenant_id == tenant_id))
            await s.commit()


async def _huellas(sm: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID) -> int:
    async with sm() as s:
        return int(
            (
                await s.execute(
                    select(func.count(OperationFingerprint.id)).where(
                        OperationFingerprint.tenant_id == tenant_id
                    )
                )
            ).scalar_one()
        )


async def test_tras_el_vencimiento_dos_claims_simultaneos_gana_uno_solo(
    sessionmaker: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> None:
    """Sin sleeps: el holder reclama (borra la vencida e inserta) y NO commitea; el
    competidor queda BLOQUEADO en la fila borrada. Tras el commit, el competidor ve
    la huella nueva y contesta duplicado, con la transacción todavía usable."""
    async with sessionmaker() as holder, sessionmaker() as competidor:
        assert await claim_operation_fingerprint_con_ventana(
            holder, tenant_id, _HUELLA, "REGISTER_SALE", _VENTANA
        )

        task = asyncio.create_task(
            claim_operation_fingerprint_con_ventana(
                competidor, tenant_id, _HUELLA, "REGISTER_SALE", _VENTANA
            )
        )
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(task), timeout=1.0)

        await holder.commit()
        assert await asyncio.wait_for(task, timeout=10.0) is False

        # La perdedora sigue viva: lee y commitea.
        await competidor.execute(text("SELECT 1"))
        await competidor.commit()

    assert await _huellas(sessionmaker, tenant_id) == 1


async def test_tras_el_vencimiento_en_paralelo_gana_exactamente_uno(
    sessionmaker: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> None:
    async def _claim() -> bool:
        async with sessionmaker() as session:
            reclamada = await claim_operation_fingerprint_con_ventana(
                session, tenant_id, _HUELLA, "REGISTER_SALE", _VENTANA
            )
            await session.commit()
            return reclamada

    resultados = await asyncio.gather(_claim(), _claim(), _claim())

    assert resultados.count(True) == 1
    assert await _huellas(sessionmaker, tenant_id) == 1
