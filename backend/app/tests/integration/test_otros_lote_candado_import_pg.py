"""I6-7 — «importar sugeridos» de «Otros» espera al import en curso del negocio.

El lote reclama las identidades de comprobante al final; sin el candado de
import (``acquire_import_lock``, el mismo que serializa los confirms) un import
en paralelo podía tomar la misma clave entre el chequeo de cada fila y el
reclamo, y el comprobante entraba dos veces. Corre sólo contra Postgres real:
en SQLite el candado es un no-op.

    TEST_PG_DSN='postgresql+asyncpg://vektor:vektor@localhost:5432/vektor_pgtest' \\
      pytest app/tests/integration/test_otros_lote_candado_import_pg.py -m postgres -n 0
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.application.services import maintenance_lock_service
from app.application.services.ingestion_import_service import bulk_import_unclassified
from app.tests.integration import _reread_scenario as esc

TEST_PG_DSN = os.environ.get("TEST_PG_DSN")

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not TEST_PG_DSN, reason="requiere PostgreSQL real (TEST_PG_DSN)"),
]

_DDL_ADVISORY_KEY = 0x5645_4B54_4F52_1E4B  # el mismo que los demás tests de candados


@pytest_asyncio.fixture(scope="module")
async def pg_engine() -> AsyncGenerator[AsyncEngine, None]:
    assert TEST_PG_DSN is not None
    engine = await esc.crear_engine(TEST_PG_DSN, _DDL_ADVISORY_KEY)
    try:
        yield engine
    finally:
        await engine.dispose()


async def test_el_lote_de_otros_espera_al_import_en_curso(pg_engine: AsyncEngine) -> None:
    sm = async_sessionmaker(pg_engine, expire_on_commit=False, autoflush=False)
    tid = uuid.uuid4()
    importando = sm()
    await maintenance_lock_service.acquire_write_lock_shared(importando, tid)
    await maintenance_lock_service.acquire_import_lock(importando, tid)  # sin commit

    async def _lote() -> None:
        async with sm() as s:
            await bulk_import_unclassified(s, tid)
            await s.commit()

    lote = asyncio.create_task(_lote())
    try:
        async with sm() as s:
            for _ in range(200):
                assert not lote.done(), "el lote no esperó al import en curso"
                esperando = await s.scalar(
                    text(
                        "SELECT count(*) FROM pg_stat_activity WHERE datname = "
                        "current_database() AND wait_event_type = 'Lock' "
                        "AND pid <> pg_backend_pid()"
                    )
                )
                if esperando:
                    break
                await asyncio.sleep(0.05)
            else:
                raise AssertionError("el lote nunca quedó esperando el candado")
    finally:
        await importando.commit()
        await importando.close()
    await asyncio.wait_for(lote, 60)
