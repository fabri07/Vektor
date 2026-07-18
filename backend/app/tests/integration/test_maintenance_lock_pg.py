"""Integración PostgreSQL del advisory lock + lease de mantenimiento (F3-T8).

Los primitivos de ``maintenance_lock_service`` que hacen la exclusión mutua REAL
son ``pg_advisory_xact_lock`` / ``pg_advisory_xact_lock_shared``: **solo existen
en Postgres**. En SQLite (la DB de la suite normal) son no-op documentados, así
que la garantía principal del dedup —que un writer y la reparación no corran a la
vez— NO se ejercita en la CI SQLite. Este módulo la prueba contra un Postgres 16
real, bloqueando el ``apply`` productivo en Neon si la barrera se rompe.

Gating: se **skippea limpio** sin ``TEST_PG_DSN`` (la CI corre en SQLite). Para
correrlo::

    TEST_PG_DSN='postgresql+asyncpg://vektor:vektor@localhost:5432/vektor' \\
        pytest app/tests/integration/test_maintenance_lock_pg.py -v --no-cov

Cómo se prueba el BLOQUEO real (sin sleeps-race): el que sostiene el lock abre su
transacción y NO commitea; el que compite corre en una task aparte sobre una
sesión/conexión SEPARADA. Se afirma el bloqueo con ``asyncio.wait_for(...,
timeout)`` → ``TimeoutError`` mientras el lock está tomado (la task se protege con
``asyncio.shield`` para que el timeout NO la cancele ni corrompa su conexión), y
luego —tras liberar por commit/rollback— la MISMA task completa sin timeout. El
bloqueo es determinístico: el competidor no puede terminar antes de la liberación,
sin importar el timing.

Aislamiento bajo xdist (la suite corre con ``-n auto`` sobre UN Postgres físico):
cada test usa un ``tenant_id`` único (fixture) y la limpieza borra SOLO las filas
de ese tenant — nunca un ``TRUNCATE`` global que pisaría el test en vuelo de otro
worker. La creación de la tabla se serializa con un advisory lock para tolerar
workers concurrentes creándola a la vez.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest
import pytest_asyncio
from sqlalchemy import Table, delete, select, text, update
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.application.services import maintenance_lock_service as svc
from app.persistence.models.maintenance_lock import TenantMaintenanceLock

TEST_PG_DSN = os.environ.get("TEST_PG_DSN")

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not TEST_PG_DSN, reason="requiere PostgreSQL real (TEST_PG_DSN)"),
]

# Clave arbitraria y estable para serializar el CREATE TABLE entre workers xdist.
_DDL_ADVISORY_KEY = 0x5645_4B54_4F52_0F38  # "VEKTOR" + F3-T8


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest_asyncio.fixture(scope="module")
async def pg_engine() -> AsyncGenerator[AsyncEngine, None]:
    """Engine asyncpg PROPIO de este test (no el global).

    ``NullPool`` → cada sesión abre su propia conexión física, garantizando que
    dos sesiones concurrentes NO compartan la misma conexión (los advisory locks
    son por conexión: compartirla invalidaría toda prueba de bloqueo).

    La tabla se crea bajo un advisory lock transaccional para que varios workers
    de xdist (mismo Postgres físico) no colisionen en el ``CREATE``. NO se dropea
    en teardown: es una DB de test y cada caso limpia sus propias filas; dropear
    global bajo xdist mataría los tests en vuelo de otros workers.
    """
    assert TEST_PG_DSN is not None  # garantizado por el skipif del pytestmark
    table = cast(Table, TenantMaintenanceLock.__table__)
    engine = create_async_engine(TEST_PG_DSN, poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _DDL_ADVISORY_KEY})
        await conn.run_sync(table.create, checkfirst=True)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def tenant_id() -> uuid.UUID:
    """Tenant único por test → aislamiento total entre casos y entre workers."""
    return uuid.uuid4()


@pytest_asyncio.fixture
async def sessionmaker(
    pg_engine: AsyncEngine, tenant_id: uuid.UUID
) -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    """Factory de sesiones + limpieza real (commit, no rollback-wrapper).

    En teardown borra SOLO las filas de este ``tenant_id`` (seguro bajo xdist).
    """
    sm = async_sessionmaker(pg_engine, expire_on_commit=False)
    try:
        yield sm
    finally:
        async with sm() as s:
            await s.execute(
                delete(TenantMaintenanceLock).where(
                    TenantMaintenanceLock.tenant_id == tenant_id
                )
            )
            await s.commit()


# ── Helpers ─────────────────────────────────────────────────────────────────


async def _acquire_exclusive_and_commit(
    sm: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> None:
    """Toma el exclusive en una sesión propia y commitea (libera) al obtenerlo."""
    async with sm() as s:
        await svc.acquire_maintenance_lock_exclusive(s, tenant_id)
        await s.commit()


async def _acquire_shared_and_commit(
    sm: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> None:
    """Toma el shared en una sesión propia y commitea (libera) al obtenerlo."""
    async with sm() as s:
        await svc.acquire_write_lock_shared(s, tenant_id)
        await s.commit()


async def _count_rows(sm: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID) -> int:
    async with sm() as s:
        rows = (
            (
                await s.execute(
                    select(TenantMaintenanceLock).where(
                        TenantMaintenanceLock.tenant_id == tenant_id
                    )
                )
            )
            .scalars()
            .all()
        )
        return len(rows)


# ── Caso 1 ──────────────────────────────────────────────────────────────────


async def test_shared_antes_bloquea_al_exclusive(
    sessionmaker: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> None:
    """SHARED tomado ANTES bloquea al EXCLUSIVE; al liberar, el exclusive entra."""
    async with sessionmaker() as holder:
        # A toma el shared y NO commitea → lo sostiene.
        await svc.acquire_write_lock_shared(holder, tenant_id)

        # B intenta el exclusive en task/sesión separada → debe BLOQUEAR.
        task = asyncio.create_task(_acquire_exclusive_and_commit(sessionmaker, tenant_id))
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(task), timeout=1.0)
        assert not task.done(), "el exclusive NO debería completar mientras el shared está tomado"

        # A libera (commit) → B ahora completa sin timeout.
        await holder.commit()
        await asyncio.wait_for(task, timeout=10.0)

    assert task.done() and task.exception() is None


# ── Caso 2 ──────────────────────────────────────────────────────────────────


async def test_writer_mientras_exclusive_tomado_bloquea(
    sessionmaker: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> None:
    """WRITER (shared) iniciado MIENTRAS el exclusive está tomado bloquea."""
    async with sessionmaker() as holder:
        # B toma el exclusive y NO commitea.
        await svc.acquire_maintenance_lock_exclusive(holder, tenant_id)

        # A intenta el shared en task separada → bloquea.
        task = asyncio.create_task(_acquire_shared_and_commit(sessionmaker, tenant_id))
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(task), timeout=1.0)
        assert not task.done(), "el shared NO debería completar mientras el exclusive está tomado"

        # B libera (rollback) → A completa.
        await holder.rollback()
        await asyncio.wait_for(task, timeout=10.0)

    assert task.done() and task.exception() is None


# ── Caso 3 ──────────────────────────────────────────────────────────────────


async def test_dos_acquire_concurrentes_gana_exactamente_uno(
    sessionmaker: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> None:
    """Dos ``acquire`` concurrentes: exactamente uno gana el lease (upsert atómico)."""

    async def _try() -> uuid.UUID | None:
        async with sessionmaker() as s:
            lease = await svc.acquire(
                s, tenant_id, reason="dedup", ttl_seconds=300, created_by="t"
            )
            await s.commit()
            return lease

    r1, r2 = await asyncio.gather(_try(), _try())
    winners = [r for r in (r1, r2) if r is not None]
    losers = [r for r in (r1, r2) if r is None]

    assert len(winners) == 1, f"exactamente uno debe ganar, ganaron {len(winners)}"
    assert len(losers) == 1, f"exactamente uno debe perder, perdieron {len(losers)}"

    # Una sola fila para el tenant, con el lease ganador.
    async with sessionmaker() as s:
        rows = (
            (
                await s.execute(
                    select(TenantMaintenanceLock).where(
                        TenantMaintenanceLock.tenant_id == tenant_id
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 1
    assert rows[0].lease_id == winners[0]


# ── Caso 4 ──────────────────────────────────────────────────────────────────


async def test_exclusive_se_libera_tras_commit_y_tras_rollback(
    sessionmaker: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> None:
    """El advisory ``_xact_`` se libera al terminar la txn (commit y rollback)."""
    # (a) commit libera → otra sesión toma sin bloquear.
    async with sessionmaker() as s1:
        await svc.acquire_maintenance_lock_exclusive(s1, tenant_id)
        await s1.commit()
    async with sessionmaker() as s2:
        # Si el commit NO hubiera liberado, esto daría TimeoutError.
        await asyncio.wait_for(
            svc.acquire_maintenance_lock_exclusive(s2, tenant_id), timeout=2.0
        )
        await s2.commit()

    # (b) rollback libera → otra sesión toma sin bloquear.
    async with sessionmaker() as s3:
        await svc.acquire_maintenance_lock_exclusive(s3, tenant_id)
        await s3.rollback()
    async with sessionmaker() as s4:
        await asyncio.wait_for(
            svc.acquire_maintenance_lock_exclusive(s4, tenant_id), timeout=2.0
        )
        await s4.commit()


# ── Caso 5 ──────────────────────────────────────────────────────────────────


async def test_lease_expirado_renew_false_y_nuevo_acquire_roba(
    sessionmaker: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> None:
    """Lease expirado: ``renew``→False, ``is_locked``→False, nuevo ``acquire`` roba."""
    async with sessionmaker() as s:
        lease = await svc.acquire(
            s, tenant_id, reason="dedup", ttl_seconds=1, created_by="t"
        )
        await s.commit()
    assert lease is not None

    # Forzar expiración de forma determinística (sin sleep): expires_at al pasado.
    past = datetime.now(UTC) - timedelta(seconds=60)
    async with sessionmaker() as s:
        await s.execute(
            update(TenantMaintenanceLock)
            .where(TenantMaintenanceLock.tenant_id == tenant_id)
            .values(expires_at=past)
        )
        await s.commit()

    async with sessionmaker() as s:
        assert await svc.renew(s, lease, ttl_seconds=300) is False
        assert await svc.is_locked(s, tenant_id) is False
        await s.commit()

    # Un nuevo acquire del mismo tenant roba el lease expirado.
    async with sessionmaker() as s:
        new_lease = await svc.acquire(
            s, tenant_id, reason="dedup-2", ttl_seconds=300, created_by="t"
        )
        await s.commit()
    assert new_lease is not None
    assert new_lease != lease

    # Sigue habiendo una sola fila (se robó, no se duplicó).
    assert await _count_rows(sessionmaker, tenant_id) == 1
