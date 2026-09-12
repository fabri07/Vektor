"""E6b — el candado, contra Postgres real: concurrencia, aislamiento y liberación.

Por qué acá y no en la suite de SQLite
--------------------------------------
Estos tres comportamientos NO se pueden probar en la suite normal:

* **Concurrencia.** El contrato es que dos confirmaciones simultáneas del mismo
  contenido no dupliquen efectos, y eso lo garantiza el UNIQUE
  ``(tenant_id, identity_key)`` — no una comprobación en memoria. Probarlo exige
  dos transacciones REALES corriendo a la vez contra la misma base; en SQLite en
  memoria, cada test tiene su propia base y no hay concurrencia que medir.
* **Aislamiento entre tenants.** Es la misma columna del mismo índice: si el
  unique estuviera sobre ``identity_key`` sola, dos negocios que reciben la misma
  factura del mismo proveedor se pisarían. Se prueba donde el índice existe.
* **Reversión parcial.** La liberación selectiva depende de que el DELETE y el
  ``NOT EXISTS`` se vean en el orden correcto dentro de la transacción.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.application.services.operation_identity_service import (
    DECISION_NUEVA,
    DECISION_YA_APLICADA,
    cerrar_plan,
    liberar_identidades_de,
    planificar_identidades,
)
from app.persistence.models.operation_identity import (
    OperationIdentity,
    OperationIdentityLink,
)
from app.persistence.models.tenant import Tenant

TEST_PG_DSN = os.environ.get("TEST_PG_DSN")

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not TEST_PG_DSN, reason="requiere PostgreSQL real (TEST_PG_DSN)"),
]

_COLS = {
    "invoice_number": "nro",
    "document_type": "tipo",
    "document_series": "pto",
    "supplier_cuit": "cuit",
    "amount": "total",
    "expense_date": "fecha",
}


def _filas(numero: str = "00000123", cuit: str = "30-71234567-8") -> list[dict[str, object]]:
    return [
        {
            "nro": numero,
            "tipo": "Factura A",
            "pto": "0001",
            "cuit": cuit,
            "total": "6000",
            "fecha": "2024-03-05",
        }
    ]


@pytest_asyncio.fixture(scope="module")
async def pg_engine() -> AsyncGenerator[AsyncEngine, None]:
    assert TEST_PG_DSN is not None
    engine = create_async_engine(TEST_PG_DSN, poolclass=NullPool)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def tenants(pg_engine: AsyncEngine) -> AsyncGenerator[tuple[uuid.UUID, uuid.UUID], None]:
    """Dos tenants reales: el aislamiento se mide entre dos, no contra uno solo."""
    a, b = uuid.uuid4(), uuid.uuid4()
    factory = async_sessionmaker(pg_engine, expire_on_commit=False)
    async with factory() as s:
        for tid in (a, b):
            s.add(Tenant(tenant_id=tid, legal_name="T", display_name="T"))
        await s.commit()
    try:
        yield a, b
    finally:
        async with factory() as s:
            for tid in (a, b):
                await s.execute(
                    delete(OperationIdentityLink).where(OperationIdentityLink.tenant_id == tid)
                )
                await s.execute(
                    delete(OperationIdentity).where(OperationIdentity.tenant_id == tid)
                )
                await s.execute(delete(Tenant).where(Tenant.tenant_id == tid))
            await s.commit()


async def _reclamar(
    factory: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    *,
    numero: str = "00000123",
    cuit: str = "30-71234567-8",
    espera: float = 0.0,
) -> str:
    """Un confirm completo simulado: reclama, "inserta" un efecto y commitea.

    ``espera`` fuerza el entrelazado: con las dos corridas dormidas entre el
    reclamo y el commit, las dos están adentro de su transacción al mismo tiempo,
    que es el escenario que el candado tiene que aguantar.
    """
    async with factory() as session:
        plan = await planificar_identidades(
            session,
            tenant_id,
            None,
            rows=_filas(numero, cuit),
            cols=_COLS,
            entity="expense",
        )
        decision = plan.decision[0]
        if decision == DECISION_NUEVA:
            # El "efecto": un uuid cualquiera. Lo que importa es que sólo la
            # corrida ganadora llegue hasta acá.
            plan.registrar_efecto(0, "expense", uuid.uuid4())
        if espera:
            await asyncio.sleep(espera)
        await cerrar_plan(session, tenant_id, None, [plan])
        await session.commit()
        return str(decision)


async def test_dos_cargas_simultaneas_no_duplican_efectos(
    pg_engine: AsyncEngine, tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """Las dos entran a la vez con el mismo comprobante. Una gana el candado y
    aplica; la otra tiene que ver "ya aplicada".

    Sin el UNIQUE esto pasaría igual con una comprobación en memoria —las dos
    leerían la tabla vacía— y quedarían dos efectos por la misma operación.
    """
    tenant_a, _ = tenants
    factory = async_sessionmaker(pg_engine, expire_on_commit=False)

    resultados = await asyncio.gather(
        _reclamar(factory, tenant_a, espera=0.15),
        _reclamar(factory, tenant_a, espera=0.15),
        return_exceptions=True,
    )
    decisiones = [r for r in resultados if isinstance(r, str)]
    fallos = [r for r in resultados if isinstance(r, BaseException)]
    assert not fallos, f"ninguna de las dos puede reventar: {fallos}"
    assert sorted(decisiones) == [DECISION_NUEVA, DECISION_YA_APLICADA], (
        f"las dos cargas simultáneas decidieron {decisiones}: una tiene que "
        "aplicar y la otra reconocer que ya estaba"
    )

    async with factory() as s:
        identidades = (
            (
                await s.execute(
                    select(OperationIdentity).where(OperationIdentity.tenant_id == tenant_a)
                )
            )
            .scalars()
            .all()
        )
        vinculos = (
            (
                await s.execute(
                    select(OperationIdentityLink).where(
                        OperationIdentityLink.tenant_id == tenant_a
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(identidades) == 1, "una operación, una identidad"
    assert len(vinculos) == 1, f"el efecto se aplicó {len(vinculos)} veces"


async def test_dos_tenants_con_el_mismo_comprobante_no_se_confunden(
    pg_engine: AsyncEngine, tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """Dos negocios pueden recibir la misma factura del mismo proveedor. Si el
    unique estuviera sobre la clave sola, el segundo la vería como ya aplicada y
    perdería la compra."""
    tenant_a, tenant_b = tenants
    factory = async_sessionmaker(pg_engine, expire_on_commit=False)

    assert await _reclamar(factory, tenant_a) == DECISION_NUEVA
    assert await _reclamar(factory, tenant_b) == DECISION_NUEVA, (
        "el segundo tenant tiene que poder cargar el MISMO comprobante"
    )

    async with factory() as s:
        for tid in (tenant_a, tenant_b):
            cuantas = len(
                (
                    await s.execute(
                        select(OperationIdentity).where(OperationIdentity.tenant_id == tid)
                    )
                )
                .scalars()
                .all()
            )
            assert cuantas == 1, f"el tenant {tid} tiene {cuantas} identidades"


async def test_una_reversion_parcial_no_libera_la_identidad(
    pg_engine: AsyncEngine, tenants: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """El caso de la relectura: un remito de dos renglones, se revierte uno y el
    otro se conserva porque el usuario lo editó.

    Si la identidad se liberara con el primer renglón revertido, el próximo
    import volvería a aplicar el documento entero **encima** del renglón que
    quedó vivo. Por eso se libera sólo cuando no queda ningún efecto.
    """
    tenant_a, _ = tenants
    factory = async_sessionmaker(pg_engine, expire_on_commit=False)
    renglon_1, renglon_2 = uuid.uuid4(), uuid.uuid4()

    async with factory() as session:
        plan = await planificar_identidades(
            session, tenant_a, None, rows=_filas(), cols=_COLS, entity="expense"
        )
        plan.registrar_efecto(0, "expense", renglon_1)
        plan.registrar_efecto(0, "expense", renglon_2)
        await cerrar_plan(session, tenant_a, None, [plan])
        await session.commit()

    async with factory() as session:
        liberadas = await liberar_identidades_de(session, tenant_a, [("expense", renglon_1)])
        await session.commit()
    assert liberadas == 0, "con un efecto vivo, la identidad NO se libera"

    async with factory() as session:
        assert (
            await _contar_identidades(session, tenant_a) == 1
        ), "la identidad tiene que seguir tomada"
        # Y el vínculo del renglón revertido sí se fue: si quedara, una futura
        # reversión total no encontraría la identidad huérfana y nunca la
        # liberaría.
        vinculos = (
            (
                await session.execute(
                    select(OperationIdentityLink).where(
                        OperationIdentityLink.tenant_id == tenant_a
                    )
                )
            )
            .scalars()
            .all()
        )
        assert [v.entity_id for v in vinculos] == [renglon_2]

    async with factory() as session:
        liberadas = await liberar_identidades_de(session, tenant_a, [("expense", renglon_2)])
        await session.commit()
    assert liberadas == 1, "sin efectos vivos, ahora sí se libera"

    async with factory() as session:
        assert await _contar_identidades(session, tenant_a) == 0

    # Y liberada de verdad: el mismo comprobante se puede volver a cargar.
    assert await _reclamar(factory, tenant_a) == DECISION_NUEVA


async def _contar_identidades(session: AsyncSession, tenant_id: uuid.UUID) -> int:
    return len(
        (
            await session.execute(
                select(OperationIdentity).where(OperationIdentity.tenant_id == tenant_id)
            )
        )
        .scalars()
        .all()
    )
