"""Anular un ticket y resolver su diferencia de stock no pueden trabarse (B11).

Qué se protege
--------------
Las dos rutas tocan **las mismas dos filas**: la cabecera de la operación y la
deuda de stock. ``void_operation`` bloquea la cabecera y después cierra las
deudas; ``resolve_stock_discrepancy`` bloqueaba primero la deuda y después la
cabecera. Con el orden cruzado, si las dos corren a la vez cada una espera el
candado que tiene la otra y Postgres aborta una con un 500 por deadlock —
justo cuando el dueño está tratando de arreglar el stock.

Por qué contra Postgres y no SQLite
-----------------------------------
Un deadlock necesita dos transacciones con conexiones físicas distintas
peleando por candados de fila. En SQLite en memoria no existe: la suite pasa y
no prueba nada. Ver ``[[feedback_sqlite_masks_postgres]]``.

El test de al lado (``test_el_orden_cruzado_si_se_traba``) corre el MISMO
escenario con el orden invertido y exige que falle. Sin él, "no hubo deadlock"
sería indistinguible de "el escenario nunca pudo producir uno".

Gating: se skippea limpio sin ``TEST_PG_DSN``. Para correrlo::

    TEST_PG_DSN='postgresql+asyncpg://vektor:vektor@localhost:5432/vektor_pgtest' \\
        pytest app/tests/integration/test_pos_deadlock_anular_resolver_pg.py -v --no-cov
"""

from __future__ import annotations

import asyncio
import inspect
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

from app.api.v1 import pos as pos_router
from app.application.services import pos_discrepancy_service
from app.persistence.models.pos_operation import (
    PosOperation,
    PosOperationLine,
)
from app.persistence.models.pos_stock_discrepancy import PosStockDiscrepancy
from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant

TEST_PG_DSN = os.environ.get("TEST_PG_DSN")

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not TEST_PG_DSN, reason="requiere PostgreSQL real (TEST_PG_DSN)"),
]

_TABLA = "pos_stock_discrepancies"


def _table_names(sync_conn: Connection) -> list[str]:
    return sa_inspect(sync_conn).get_table_names()


@pytest_asyncio.fixture(scope="module")
async def pg_engine() -> AsyncGenerator[AsyncEngine, None]:
    """NullPool: sin él las dos sesiones comparten conexión y no hay contención."""
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
async def escenario(
    pg_engine: AsyncEngine,
) -> AsyncGenerator[tuple[async_sessionmaker[AsyncSession], uuid.UUID, uuid.UUID, uuid.UUID], None]:
    """Un ticket cobrado con salida pendiente: la cabecera y su deuda."""
    sm = async_sessionmaker(pg_engine, expire_on_commit=False)
    tenant_id = uuid.uuid4()
    operacion_id = uuid.uuid4()
    discrepancia_id = uuid.uuid4()
    producto_id = uuid.uuid4()
    linea_id = uuid.uuid4()

    async with sm() as s:
        s.add(Tenant(tenant_id=tenant_id, legal_name="T", display_name="T"))
        await s.flush()
        s.add(
            Product(
                id=producto_id,
                tenant_id=tenant_id,
                name="Yerba",
                sale_price_ars=Decimal("100.00"),
                stock_units=0,
            )
        )
        s.add(
            PosOperation(
                id=operacion_id,
                tenant_id=tenant_id,
                client_operation_id=f"op-{uuid.uuid4().hex[:12]}",
                operation_date=datetime.now(UTC).replace(tzinfo=None),
                subtotal_ars=Decimal("500.00"),
                total_ars=Decimal("500.00"),
            )
        )
        await s.flush()
        s.add(
            PosOperationLine(
                id=linea_id,
                tenant_id=tenant_id,
                operation_id=operacion_id,
                product_id=producto_id,
                position=0,
                quantity=5,
                unit_price_list=Decimal("100.00"),
                line_total_ars=Decimal("500.00"),
            )
        )
        await s.flush()
        s.add(
            PosStockDiscrepancy(
                id=discrepancia_id,
                tenant_id=tenant_id,
                operation_id=operacion_id,
                line_id=linea_id,
                product_id=producto_id,
                pending_units=5,
                stock_at_capture=0,
            )
        )
        await s.commit()

    try:
        yield sm, tenant_id, operacion_id, discrepancia_id
    finally:
        async with sm() as s:
            await s.execute(
                delete(PosStockDiscrepancy).where(
                    PosStockDiscrepancy.tenant_id == tenant_id
                )
            )
            await s.execute(
                delete(PosOperationLine).where(PosOperationLine.tenant_id == tenant_id)
            )
            await s.execute(delete(PosOperation).where(PosOperation.tenant_id == tenant_id))
            await s.execute(delete(Product).where(Product.tenant_id == tenant_id))
            await s.execute(delete(Tenant).where(Tenant.tenant_id == tenant_id))
            await s.commit()


async def _bloquear_operacion(s: AsyncSession, tenant_id: uuid.UUID, op_id: uuid.UUID) -> None:
    await s.execute(
        select(PosOperation)
        .where(PosOperation.tenant_id == tenant_id, PosOperation.id == op_id)
        .with_for_update()
    )


async def _bloquear_discrepancia(
    s: AsyncSession, tenant_id: uuid.UUID, disc_id: uuid.UUID
) -> None:
    await pos_discrepancy_service.obtener(s, tenant_id, disc_id, para_actualizar=True)


async def _correr(
    sm: async_sessionmaker[AsyncSession],
    primero,
    segundo,
    puerta: asyncio.Event,
    listo: asyncio.Event,
) -> None:
    """Toma el primer candado, avisa, espera a la otra tarea y toma el segundo."""
    async with sm() as s:
        await primero(s)
        listo.set()
        await asyncio.wait_for(puerta.wait(), timeout=10)
        await segundo(s)
        await s.commit()


async def test_anular_y_resolver_a_la_vez_no_se_traban(
    escenario: tuple[async_sessionmaker[AsyncSession], uuid.UUID, uuid.UUID, uuid.UUID],
) -> None:
    """Las dos rutas bloquean en el MISMO orden: ticket y después deuda."""
    sm, tenant_id, op_id, disc_id = escenario
    a_listo, b_listo = asyncio.Event(), asyncio.Event()
    puerta = asyncio.Event()

    async def anular() -> None:
        await _correr(
            sm,
            lambda s: _bloquear_operacion(s, tenant_id, op_id),
            lambda s: _bloquear_discrepancia(s, tenant_id, disc_id),
            puerta,
            a_listo,
        )

    async def resolver() -> None:
        # El mismo orden que `resolve_stock_discrepancy`: la lectura sin candado
        # para saber qué ticket bloquear no toma ninguno, así que no cuenta.
        async def primero(s: AsyncSession) -> None:
            await pos_discrepancy_service.obtener(s, tenant_id, disc_id)
            await _bloquear_operacion(s, tenant_id, op_id)

        await _correr(
            sm,
            primero,
            lambda s: _bloquear_discrepancia(s, tenant_id, disc_id),
            puerta,
            b_listo,
        )

    tareas = asyncio.gather(anular(), resolver())
    # Las dos empiezan por la cabecera, así que una queda esperando a la otra:
    # con que UNA llegue a su primer candado alcanza para abrir la puerta.
    espera = [asyncio.create_task(a_listo.wait()), asyncio.create_task(b_listo.wait())]
    _hechas, pendientes = await asyncio.wait(
        espera, timeout=10, return_when=asyncio.FIRST_COMPLETED
    )
    for tarea in pendientes:
        tarea.cancel()
    assert a_listo.is_set() or b_listo.is_set(), "ninguna llegó a tomar el primer candado"
    puerta.set()
    await asyncio.wait_for(tareas, timeout=20)


async def test_el_orden_cruzado_si_se_traba(
    escenario: tuple[async_sessionmaker[AsyncSession], uuid.UUID, uuid.UUID, uuid.UUID],
) -> None:
    """El escenario SÍ puede producir un deadlock: es lo que hace válido al de arriba.

    Sin esto, "no se trabaron" podría significar "estas dos cosas nunca
    compiten", y el test de al lado pasaría aunque el orden volviera a
    invertirse.
    """
    sm, tenant_id, op_id, disc_id = escenario
    a_listo, b_listo = asyncio.Event(), asyncio.Event()
    puerta = asyncio.Event()

    async def anular() -> None:
        await _correr(
            sm,
            lambda s: _bloquear_operacion(s, tenant_id, op_id),
            lambda s: _bloquear_discrepancia(s, tenant_id, disc_id),
            puerta,
            a_listo,
        )

    async def resolver_al_reves() -> None:
        await _correr(
            sm,
            lambda s: _bloquear_discrepancia(s, tenant_id, disc_id),
            lambda s: _bloquear_operacion(s, tenant_id, op_id),
            puerta,
            b_listo,
        )

    tareas = asyncio.gather(anular(), resolver_al_reves(), return_exceptions=True)
    await asyncio.wait_for(asyncio.gather(a_listo.wait(), b_listo.wait()), timeout=10)
    puerta.set()
    resultados = await asyncio.wait_for(tareas, timeout=20)
    assert any(isinstance(r, Exception) for r in resultados), (
        "con el orden cruzado tiene que abortar una: si no, este escenario no "
        "prueba nada sobre el orden"
    )


def test_el_handler_bloquea_el_ticket_antes_que_la_diferencia() -> None:
    """Compuerta de ORDEN sobre el código del handler.

    El test de concurrencia prueba que ese orden no se traba; éste prueba que el
    handler sigue usándolo. Reordenar las dos líneas volvería a abrir el
    deadlock sin que ningún otro test lo note.
    """
    fuente = inspect.getsource(pos_router.resolve_stock_discrepancy)
    ticket = fuente.index("_cargar_operacion")
    # El ÚLTIMO `obtener(` es el que toma el candado; el primero es la lectura
    # sucia que sólo sirve para saber qué ticket bloquear.
    con_candado = fuente.rindex("obtener(")
    assert ticket < con_candado, (
        "resolve_stock_discrepancy tiene que bloquear la operación ANTES de "
        "bloquear la discrepancia (mismo orden que void_operation)"
    )
