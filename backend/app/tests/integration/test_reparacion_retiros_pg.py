"""D·3 contra Postgres: dos corridas a la vez no crean dos retiros por gasto.

``owner_capital_movements.source_ref`` no tiene restricción única: lo que evita el
doble movimiento es el lock exclusivo de mantenimiento más la revalidación bajo
``FOR UPDATE``. SQLite no tiene ninguno de los dos; acá cada corrida va en su
propia conexión y commitea de verdad.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncGenerator
from datetime import date
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.application.services import reparacion_retiros_service as svc
from app.persistence.models.audit import DecisionAuditLog
from app.persistence.models.owner_capital import OwnerCapitalMovement
from app.persistence.models.repair import DataRepairItem, DataRepairRun
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import ExpenseEntry
from app.tests.api.v1.test_owner_capital import _gasto_retiro_viejo
from app.tests.integration import _reread_scenario as esc

TEST_PG_DSN = os.environ.get("TEST_PG_DSN")

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not TEST_PG_DSN, reason="requiere TEST_PG_DSN (Postgres real)"),
]

_DDL_ADVISORY_KEY = 0x5645_4B54_4F52_4433  # "VEKTOR" + D3


@pytest_asyncio.fixture(scope="module")
async def pg_engine() -> AsyncGenerator[AsyncEngine, None]:
    assert TEST_PG_DSN is not None
    engine = await esc.crear_engine(TEST_PG_DSN, _DDL_ADVISORY_KEY)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def fabrica(pg_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(pg_engine, expire_on_commit=False)


@pytest_asyncio.fixture
async def tenant(fabrica: async_sessionmaker[AsyncSession]) -> AsyncGenerator[Tenant, None]:
    tid = uuid.uuid4()
    async with fabrica() as s:
        t = Tenant(tenant_id=tid, legal_name="D3", display_name="D3")
        s.add(t)
        await s.flush()
        for monto, dia, medio in (("1000", 3, "cash"), ("2500", 7, "transfer")):
            s.add(_gasto_retiro_viejo(t, Decimal(monto), date(2026, 9, dia), medio))
        await s.commit()
    try:
        yield t
    finally:
        async with fabrica() as s:
            runs = select(DataRepairRun.id).where(DataRepairRun.tenant_id == tid)
            await s.execute(delete(DataRepairItem).where(DataRepairItem.run_id.in_(runs)))
            for modelo in (DataRepairRun, DecisionAuditLog, OwnerCapitalMovement, ExpenseEntry):
                await s.execute(delete(modelo).where(modelo.tenant_id == tid))
            await s.execute(delete(Tenant).where(Tenant.tenant_id == tid))
            await s.commit()


async def test_dos_corridas_a_la_vez_dejan_un_retiro_por_gasto(
    fabrica: async_sessionmaker[AsyncSession], tenant: Tenant
) -> None:
    tid = tenant.tenant_id

    async def corrida() -> int:
        async with fabrica() as s:
            r = await svc.reparar(s, tid, aplicar=True)
            await s.commit()
            return len(r.candidatos)

    convertidos = await asyncio.gather(corrida(), corrida())

    # Una corrida convierte los dos; la otra espera el lock y ya no encuentra nada.
    assert sorted(convertidos) == [0, 2]
    async with fabrica() as s:
        por_gasto = (
            await s.execute(
                select(OwnerCapitalMovement.source_ref, func.count())
                .where(OwnerCapitalMovement.tenant_id == tid)
                .group_by(OwnerCapitalMovement.source_ref)
            )
        ).tuples().all()
        assert sorted(n for _, n in por_gasto) == [1, 1]
        vivos = await s.scalar(
            select(func.count()).select_from(ExpenseEntry).where(
                ExpenseEntry.tenant_id == tid, ExpenseEntry.voided_at.is_(None)
            )
        )
        assert vivos == 0
