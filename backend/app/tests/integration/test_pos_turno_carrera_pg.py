"""La carrera por crear el turno de caja no se lleva puesta la venta (B8).

Qué se protege
--------------
El turno lo crea quien llega primero: la apertura explícita o la primera venta
que lo adopta. Las dos pueden correr a la vez y el UNIQUE
``(tenant, client_session_id)`` decide. Cuando la VENTA pierde esa carrera, ya
hizo flush de su operación, sus líneas, sus pagos y su reclamo de idempotencia;
``abrir_o_adoptar`` hacía ``session.rollback()`` y se llevaba todo eso puesto.
Ahora el INSERT del turno va en un SAVEPOINT y el resto de la transacción queda
intacto.

Por qué contra Postgres
-----------------------
La carrera necesita dos transacciones con conexiones físicas distintas: la
segunda tiene que quedar esperando el UNIQUE que la primera todavía no
commiteó. En SQLite en memoria no se reproduce.

Gating: se skippea limpio sin ``TEST_PG_DSN``. Para correrlo::

    TEST_PG_DSN='postgresql+asyncpg://vektor:vektor@localhost:5432/vektor_pgtest' \\
        pytest app/tests/integration/test_pos_turno_carrera_pg.py -v --no-cov -n 0
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from decimal import Decimal

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

from app.application.services import pos_cash_session_service
from app.persistence.models.pos_cash_session import PosCashSession
from app.persistence.models.pos_operation import PosOperation
from app.persistence.models.tenant import Tenant

TEST_PG_DSN = os.environ.get("TEST_PG_DSN")

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not TEST_PG_DSN, reason="requiere PostgreSQL real (TEST_PG_DSN)"),
]

_TABLA = "pos_cash_sessions"


def _table_names(sync_conn: Connection) -> list[str]:
    return sa_inspect(sync_conn).get_table_names()


@pytest_asyncio.fixture(scope="module")
async def pg_engine() -> AsyncGenerator[AsyncEngine, None]:
    """NullPool: sin él las dos sesiones comparten conexión y no hay carrera."""
    assert TEST_PG_DSN is not None
    engine = create_async_engine(TEST_PG_DSN, poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.execute(text("SELECT 1"))
        if _TABLA not in await conn.run_sync(_table_names):
            pytest.skip(f"schema sin migrar (falta {_TABLA}): correr alembic upgrade head")
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def negocio(
    pg_engine: AsyncEngine,
) -> AsyncGenerator[tuple[async_sessionmaker[AsyncSession], uuid.UUID], None]:
    sm = async_sessionmaker(pg_engine, expire_on_commit=False)
    tenant_id = uuid.uuid4()
    async with sm() as s:
        s.add(Tenant(tenant_id=tenant_id, legal_name="T", display_name="T"))
        await s.commit()
    try:
        yield sm, tenant_id
    finally:
        async with sm() as s:
            await s.execute(delete(PosOperation).where(PosOperation.tenant_id == tenant_id))
            await s.execute(
                delete(PosCashSession).where(PosCashSession.tenant_id == tenant_id)
            )
            await s.execute(delete(Tenant).where(Tenant.tenant_id == tenant_id))
            await s.commit()


def _operacion(tenant_id: uuid.UUID, turno: str) -> PosOperation:
    return PosOperation(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        client_operation_id=f"op-{uuid.uuid4().hex[:12]}",
        client_session_id=turno,
        operation_date=datetime.now(UTC).replace(tzinfo=None),
        subtotal_ars=Decimal("100.00"),
        total_ars=Decimal("100.00"),
    )


async def test_la_venta_que_pierde_la_carrera_del_turno_sobrevive(
    negocio: tuple[async_sessionmaker[AsyncSession], uuid.UUID],
) -> None:
    """La apertura crea el turno sin commitear; la venta ya hizo flush y queda
    esperando el UNIQUE. Al commitear la apertura, la venta pierde la carrera:
    tiene que adoptar el turno ganador y conservar su operación."""
    sm, tenant_id = negocio
    turno = f"turno-{uuid.uuid4().hex[:12]}"

    async with sm() as apertura, sm() as venta:
        ganador = await pos_cash_session_service.abrir_o_adoptar(
            apertura, tenant_id, turno, user_id=None, terminal_id=None
        )
        await apertura.flush()

        operacion = _operacion(tenant_id, turno)
        venta.add(operacion)
        await venta.flush()

        adopcion = asyncio.create_task(
            pos_cash_session_service.abrir_o_adoptar(
                venta, tenant_id, turno, user_id=None, terminal_id=None
            )
        )
        # La venta tiene que llegar a esperar el UNIQUE antes del commit: si no,
        # vería la fila ya commiteada y no habría carrera que probar.
        await asyncio.sleep(0.5)
        assert not adopcion.done(), "la venta no quedó esperando el UNIQUE"
        await apertura.commit()

        adoptado = await asyncio.wait_for(adopcion, timeout=10)
        assert adoptado.id == ganador.id
        await venta.commit()

    async with sm() as s:
        guardada = (
            await s.execute(select(PosOperation).where(PosOperation.id == operacion.id))
        ).scalar_one_or_none()
        turnos = (
            await s.execute(
                select(PosCashSession).where(
                    PosCashSession.tenant_id == tenant_id,
                    PosCashSession.client_session_id == turno,
                )
            )
        ).scalars().all()
    assert guardada is not None, "la carrera del turno se llevó puesta la venta"
    assert len(turnos) == 1
