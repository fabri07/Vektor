"""A-4 contra PostgreSQL real: la anulación y el cierre del turno no se pisan.

Qué se protege
--------------
La anulación relee el turno con ``FOR SHARE`` DESPUÉS de tocar los productos, y
el cierre lo toma con ``FOR UPDATE``. Así:

- un reintegro en vuelo hace esperar al cierre, que después lo resta;
- un reintegro que llega tarde espera al cierre y ve el turno CERRADO
  (``populate_existing``), así que se rechaza en vez de quedar fuera del
  arqueo firmado;
- una anulación del mismo turno y el mismo día hace esperar al cierre, que
  después ya no suma el ticket anulado (B-3).

Por qué contra Postgres: hacen falta dos transacciones con conexiones físicas
distintas; en SQLite en memoria no hay candados de fila.

Gating: se skippea limpio sin ``TEST_PG_DSN``. Para correrlo::

    TEST_PG_DSN='postgresql+asyncpg://vektor:vektor@localhost:5432/vektor_pgtest' \\
        pytest app/tests/integration/test_pos_anulacion_reintegro_a4_pg.py -v --no-cov -n 0
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

from app.application.services import pos_cash_session_service as svc
from app.domain.business_time import today_ar
from app.persistence.models.pos_cash_session import PosCashRefund, PosCashSession
from app.persistence.models.pos_operation import PosOperation, PosTender
from app.persistence.models.tenant import Tenant

TEST_PG_DSN = os.environ.get("TEST_PG_DSN")

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not TEST_PG_DSN, reason="requiere PostgreSQL real (TEST_PG_DSN)"),
]

_TABLA = "pos_cash_refunds"


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
            for modelo in (PosCashRefund, PosTender, PosOperation, PosCashSession):
                await s.execute(delete(modelo).where(modelo.tenant_id == tenant_id))
            await s.execute(delete(Tenant).where(Tenant.tenant_id == tenant_id))
            await s.commit()


async def _venta(s: AsyncSession, tenant_id: uuid.UUID, turno: str) -> PosOperation:
    op = PosOperation(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        client_operation_id=f"op-{uuid.uuid4().hex[:12]}",
        client_session_id=turno,
        operation_date=datetime.now(UTC).replace(tzinfo=None),
        subtotal_ars=Decimal("500"),
        total_ars=Decimal("500"),
        status="COMPLETED",
    )
    s.add(op)
    await s.flush()
    s.add(
        PosTender(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            operation_id=op.id,
            position=0,
            payment_method="cash",
            amount_ars=Decimal("500"),
        )
    )
    await s.flush()
    return op


async def _abrir(s: AsyncSession, tenant_id: uuid.UUID, turno: str, fondo: str) -> PosCashSession:
    return await svc.abrir_o_adoptar(
        s, tenant_id, turno, user_id=None, terminal_id=None, fondo_declarado=Decimal(fondo)
    )


async def _cerrar(s: AsyncSession, sid: uuid.UUID) -> PosCashSession:
    fila = (
        await s.execute(
            select(PosCashSession)
            .where(PosCashSession.id == sid)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    return await svc.cerrar(s, fila, counted_cash_ars=Decimal("500"), user_id=None)


async def _turno_cerrado_y_otro_abierto(
    sm: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> tuple[uuid.UUID, uuid.UUID]:
    """t1 vendió $500 y cerró; t2 abrió con $1.000. El ticket se devuelve desde t2."""
    async with sm() as s:
        await _abrir(s, tenant_id, "t1", "0")
        op = await _venta(s, tenant_id, "t1")
        t1 = await svc.obtener_por_client_id(s, tenant_id, "t1", para_actualizar=True)
        assert t1 is not None
        await svc.cerrar(s, t1, counted_cash_ars=Decimal("500"), user_id=None)
        t2 = await _abrir(s, tenant_id, "t2", "1000")
        await s.commit()
        return op.id, t2.id


async def test_el_cierre_espera_al_reintegro_en_vuelo_y_lo_resta(
    negocio: tuple[async_sessionmaker[AsyncSession], uuid.UUID],
) -> None:
    sm, tenant_id = negocio
    op_id, t2_id = await _turno_cerrado_y_otro_abierto(sm, tenant_id)
    async with sm() as anulacion, sm() as cierre:
        op = await anulacion.get(PosOperation, op_id, with_for_update=True)
        assert op is not None
        op.status = "VOIDED"
        await anulacion.flush()
        turno = await svc.obtener_por_client_id(anulacion, tenant_id, "t2", compartido=True)
        assert turno is not None and turno.esta_abierto
        await svc.registrar_reintegro(
            anulacion,
            op,
            sesion=turno,
            cash_ars=Decimal("500"),
            refund_date=today_ar(),
            user_id=None,
        )
        tarea = asyncio.create_task(_cerrar(cierre, t2_id))
        await asyncio.sleep(0.5)
        assert not tarea.done(), "el cierre no esperó al reintegro"
        await anulacion.commit()
        cerrado = await asyncio.wait_for(tarea, 10)
        await cierre.commit()
    assert cerrado.expected_cash_ars == Decimal("500.00")


async def test_el_reintegro_tardio_espera_al_cierre_y_ve_el_turno_cerrado(
    negocio: tuple[async_sessionmaker[AsyncSession], uuid.UUID],
) -> None:
    sm, tenant_id = negocio
    _op_id, t2_id = await _turno_cerrado_y_otro_abierto(sm, tenant_id)
    async with sm() as anulacion, sm() as cierre:
        # La decisión se toma sin candado, como en el handler.
        sin_candado = await svc.obtener_por_client_id(anulacion, tenant_id, "t2")
        assert sin_candado is not None and sin_candado.esta_abierto
        await _cerrar(cierre, t2_id)
        tarea = asyncio.create_task(
            svc.obtener_por_client_id(anulacion, tenant_id, "t2", compartido=True)
        )
        await asyncio.sleep(0.5)
        assert not tarea.done(), "el FOR SHARE no esperó al cierre"
        await cierre.commit()
        turno = await asyncio.wait_for(tarea, 10)
        assert turno is not None and not turno.esta_abierto, "populate_existing no refrescó"
        await anulacion.rollback()


async def test_anular_en_el_mismo_turno_hace_esperar_al_cierre(
    negocio: tuple[async_sessionmaker[AsyncSession], uuid.UUID],
) -> None:
    """B-3: mismo turno y mismo día, sin reintegro: el cierre no suma el ticket anulado."""
    sm, tenant_id = negocio
    async with sm() as s:
        t1 = await _abrir(s, tenant_id, "t1", "0")
        op = await _venta(s, tenant_id, "t1")
        await s.commit()
    async with sm() as anulacion, sm() as cierre:
        fila = await anulacion.get(PosOperation, op.id, with_for_update=True)
        assert fila is not None
        fila.status = "VOIDED"
        await anulacion.flush()
        turno = await svc.obtener_por_client_id(anulacion, tenant_id, "t1", compartido=True)
        assert turno is not None and turno.esta_abierto
        tarea = asyncio.create_task(_cerrar(cierre, t1.id))
        await asyncio.sleep(0.5)
        assert not tarea.done(), "el cierre no esperó a la anulación"
        await anulacion.commit()
        cerrado = await asyncio.wait_for(tarea, 10)
        await cierre.commit()
    assert cerrado.expected_cash_ars == Decimal("0.00")
