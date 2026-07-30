"""Integración PostgreSQL de la carrera real del guard anti-duplicado de
``reread_service.start_background_apply`` (Task 1 de F9b).

El guard usa ``pg_advisory_xact_lock`` para serializar el chequeo "¿ya hay una
relectura RUNNING?" + la creación del nuevo ``DataRepairRun``. En SQLite (la
suite normal) NO hay concurrencia real: dos requests no pueden correr en
paralelo, así que la garantía central del guard —que dos ``start_background_
apply`` concurrentes del MISMO archivo nunca creen dos runs RUNNING— NO se
ejercita en la CI SQLite. Este módulo la prueba contra un Postgres real. Ver
``[[feedback_sqlite_masks_postgres]]`` y el mismo patrón en
``test_ingestion_lease_pg.py``.

Gating: se **skippea limpio** sin ``TEST_PG_DSN``. Para correrlo::

    TEST_PG_DSN='postgresql+asyncpg://vektor:vektor@localhost:55432/vektor' \\
        pytest app/tests/integration/test_reread_concurrency_pg.py -v --no-cov
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncGenerator
from typing import cast

import pytest
import pytest_asyncio
from sqlalchemy import Table, delete, select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.application.services import reread_service
from app.persistence.db.base import Base
from app.persistence.models.file import PROCESSING_STATUS_DONE, UploadedFile
from app.persistence.models.repair import DataRepairRun
from app.persistence.models.tenant import Tenant

TEST_PG_DSN = os.environ.get("TEST_PG_DSN")

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not TEST_PG_DSN, reason="requiere TEST_PG_DSN (Postgres real)"),
]

# Clave arbitraria y estable para serializar el CREATE entre workers xdist — namespace
# propio (distinto del de ``test_ingestion_lease_pg.py``, aunque ambos son no-op entre
# sí: son locks independientes sobre el mismo valor de clave, Postgres no los mezcla).
_DDL_ADVISORY_KEY = 0x5645_4B54_4F52_0F90  # "VEKTOR" + F9b


@pytest_asyncio.fixture(scope="module")
async def pg_engine() -> AsyncGenerator[AsyncEngine, None]:
    """Engine asyncpg propio (NullPool → cada sesión, su conexión física).

    Crea SOLO ``tenants`` + ``uploaded_files`` + ``data_repair_runs``
    (checkfirst, idempotente) — igual criterio que ``test_ingestion_lease_pg.py``:
    en CI, ``alembic upgrade head`` ya creó el schema real ANTES y este
    checkfirst queda no-op.
    """
    assert TEST_PG_DSN is not None
    engine = create_async_engine(TEST_PG_DSN, poolclass=NullPool)
    tables: list[Table] = [
        cast("Table", Tenant.__table__),
        cast("Table", UploadedFile.__table__),
        cast("Table", DataRepairRun.__table__),
    ]
    async with engine.begin() as conn:
        from sqlalchemy import text  # noqa: PLC0415

        await conn.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _DDL_ADVISORY_KEY})
        await conn.run_sync(Base.metadata.create_all, tables=tables, checkfirst=True)
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
    try:
        yield sm
    finally:
        async with sm() as s:
            await s.execute(delete(DataRepairRun).where(DataRepairRun.tenant_id == tenant_id))
            await s.execute(delete(UploadedFile).where(UploadedFile.tenant_id == tenant_id))
            await s.execute(delete(Tenant).where(Tenant.tenant_id == tenant_id))
            await s.commit()


async def _seed_file(
    sm: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
) -> uuid.UUID:
    """Crea (commit) un tenant + un uploaded_file DONE y devuelve el file_id —
    ``start_background_apply`` valida que el archivo exista/pertenezca antes de
    encolar (ver ``reread_service._load_file``)."""
    file_id = uuid.uuid4()
    async with sm() as s:
        # Flush del tenant ANTES del archivo: sin relationship() la unit-of-work
        # no ordena sola el INSERT del padre → FK violation si van juntos.
        s.add(Tenant(tenant_id=tenant_id, legal_name="T", display_name="T"))
        await s.flush()
        s.add(
            UploadedFile(
                id=file_id,
                tenant_id=tenant_id,
                original_filename="f.xlsx",
                s3_key="k",
                content_type="application/vnd.ms-excel",
                size_bytes=1,
                purpose="ventas",
                processing_status=PROCESSING_STATUS_DONE,
            )
        )
        await s.commit()
    return file_id


async def _attempt(
    sm: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    file_id: uuid.UUID,
    results: list[tuple[str, uuid.UUID | None]],
) -> None:
    """Un intento de ``start_background_apply`` en su PROPIA sesión/conexión —
    la garantía del guard depende del row-lock/advisory-lock de Postgres, que
    solo se ejercita de verdad entre conexiones físicas distintas (no dentro de
    la misma sesión secuencial, que es lo único que SQLite puede probar)."""
    async with sm() as session:
        try:
            run = await reread_service.start_background_apply(session, file_id, tenant_id)
            await session.commit()
            results.append(("ok", run.id))
        except ValueError:
            await session.rollback()
            results.append(("blocked", None))


async def test_dos_start_background_apply_concurrentes_solo_uno_gana(
    sessionmaker: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> None:
    """Dos ``start_background_apply`` concurrentes (conexiones separadas) del
    MISMO archivo: exactamente uno crea el ``DataRepairRun`` RUNNING, el otro
    pierde con ``ValueError`` (409 en el endpoint). Sin el advisory lock del
    guard, ambas transacciones podrían leer "no hay RUNNING" antes de que la
    otra commitee su INSERT → dos runs RUNNING duplicados (el bug que este test
    existe para cazar — ver Task 1 de F9b)."""
    file_id = await _seed_file(sessionmaker, tenant_id)

    results: list[tuple[str, uuid.UUID | None]] = []
    await asyncio.gather(
        _attempt(sessionmaker, tenant_id, file_id, results),
        _attempt(sessionmaker, tenant_id, file_id, results),
    )

    oks = [r for r in results if r[0] == "ok"]
    blocked = [r for r in results if r[0] == "blocked"]
    assert len(oks) == 1, f"esperaba exactamente 1 'ok', obtuve: {results}"
    assert len(blocked) == 1, f"esperaba exactamente 1 'blocked', obtuve: {results}"

    # Confirmación directa en la DB (no solo por el resultado en memoria): a lo
    # sumo un DataRepairRun RUNNING para este tenant/archivo.
    async with sessionmaker() as s:
        rows = (
            await s.execute(
                select(DataRepairRun).where(
                    DataRepairRun.tenant_id == tenant_id,
                    DataRepairRun.status == "RUNNING",
                )
            )
        ).scalars().all()
    assert len(rows) == 1
    assert rows[0].id == oks[0][1]
