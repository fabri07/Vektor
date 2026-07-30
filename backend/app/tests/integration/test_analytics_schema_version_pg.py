"""Integración PostgreSQL: los eventos v1 no mueven los percentiles observados.

Por qué contra Postgres real
----------------------------
La garantía que importa no es "el método devuelve None con datos viejos" —eso lo
corta el conteo, que es SQL portable y ya está cubierto en
``app/tests/application/test_analytics_sample_integrity.py``. La garantía que
importa es que, en una muestra MIXTA, los percentiles salen calculados **solo**
sobre las filas nuevas. Eso pasa por ``percentile_cont``, que SQLite no tiene:
en SQLite la consulta ni siquiera llega a ejecutarse.

Es exactamente el escenario de producción. La tabla ya tiene filas v1 con ceros
fabricados y va a seguir recibiendo v2: el filtro tiene que estar en LAS DOS
consultas del método —la del conteo y la de los percentiles—, y solo un motor con
``percentile_cont`` puede notar si falta en la segunda. Ver
``[[feedback_sqlite_masks_postgres]]``.

Aislamiento bajo xdist — DOS reglas, las dos aprendidas rompiendo la suite
--------------------------------------------------------------------------
1. **Código de rubro único por test.** La columna es texto libre sin FK ni CHECK,
   así que un código inventado no choca con otro worker ni con datos reales.

2. **Ninguna transacción sobrevive a la operación que la abrió.** Esta es la
   cara: la suite ``postgres`` incluye los tests de F5-B, que hacen ``CREATE
   UNIQUE INDEX CONCURRENTLY``, y CIC espera a que terminen TODAS las
   transacciones concurrentes de la base — también las de otra tabla, también las
   de solo lectura. Una primera versión de este archivo tenía una sesión de
   fixture que vivía todo el test: el ``SELECT`` de percentiles dejaba una
   transacción de lectura abierta hasta el teardown, los CIC de los otros workers
   se colgaban esperándola, y la suite entera quedó 48 minutos al 0% de CPU sin
   fallar ni terminar. Por eso acá cada operación abre su sesión, commitea y
   cierra: `_sesion_corta`.

Gating: se **skippea limpio** sin ``TEST_PG_DSN``. Para correrlo::

    TEST_PG_DSN='postgresql+asyncpg://vektor:vektor@localhost:5432/vektor' \\
        pytest app/tests/integration/test_analytics_schema_version_pg.py -v --no-cov
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.persistence.models.analytics_event import EVENT_SCHEMA_VERSION, AnalyticsEvent
from app.persistence.repositories.analytics_repository import AnalyticsRepository

TEST_PG_DSN = os.environ.get("TEST_PG_DSN")

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not TEST_PG_DSN, reason="requiere PostgreSQL real (TEST_PG_DSN)"),
]

#: Margen de las filas VIEJAS: el cero fabricado que escribía el código v1 para
#: todo negocio sin ventas. Es el valor que este filtro existe para excluir.
_MARGEN_FABRICADO = 0.0

#: Margen de las filas NUEVAS. Lejos del cero a propósito: si una sola v1 se
#: colara, los percentiles se desplomarían y la aserción lo vería.
_MARGENES_REALES = [0.10, 0.11, 0.12, 0.13, 0.14, 0.15]


@pytest_asyncio.fixture
async def engine_pg() -> AsyncGenerator[AsyncEngine, None]:
    engine = create_async_engine(TEST_PG_DSN or "", poolclass=NullPool)
    yield engine
    await engine.dispose()


@asynccontextmanager
async def _sesion_corta(engine: AsyncEngine) -> AsyncGenerator[AsyncSession, None]:
    """Sesión que no sobrevive a la operación: commitea y cierra al salir.

    El ``commit()`` cierra también las transacciones de SOLO LECTURA, que son las
    que colgaban a los ``CREATE INDEX CONCURRENTLY`` de los otros workers (ver el
    docstring del módulo). No alcanza con no escribir: alcanza con no dejar nada
    abierto.
    """
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as session:
        try:
            yield session
            await session.commit()
        finally:
            await session.rollback()


@pytest_asyncio.fixture
async def rubro_scratch(engine_pg: AsyncEngine) -> AsyncGenerator[str, None]:
    """Código de rubro inventado y único, borrado al final."""
    code = f"_test_{uuid.uuid4().hex[:12]}"
    yield code
    async with _sesion_corta(engine_pg) as session:
        await session.execute(
            text("DELETE FROM analytics_events WHERE vertical_code = :vc"), {"vc": code}
        )


async def _sembrar(
    engine: AsyncEngine,
    *,
    vertical_code: str,
    margenes: list[float],
    schema_version: int,
) -> None:
    async with _sesion_corta(engine) as session:
        for margen in margenes:
            session.add(
                AnalyticsEvent(
                    vertical_code=vertical_code,
                    score_total=70,
                    score_cash=70,
                    score_margin=70,
                    score_stock=70,
                    score_supplier=70,
                    margin_ratio=margen,
                    cash_ratio=1.5,
                    supplier_count=3,
                    product_count=10,
                    low_stock_pct=0.0,
                    data_completeness=80.0,
                    schema_version=schema_version,
                    created_at=datetime.now(UTC),
                )
            )


@pytest.mark.asyncio
async def test_los_percentiles_ignoran_las_filas_viejas(
    engine_pg: AsyncEngine, rubro_scratch: str
) -> None:
    """Muestra mixta: doce filas, seis con ceros fabricados. Los ceros no cuentan.

    Sin el filtro en la consulta de percentiles, la mitad de la muestra sería
    ``0.0`` y p10/p25 caerían a cero — que es precisamente la falsa alarma
    sectorial que este mecanismo existe para no fabricar.
    """
    await _sembrar(
        engine_pg,
        vertical_code=rubro_scratch,
        margenes=[_MARGEN_FABRICADO] * 6,
        schema_version=1,
    )
    await _sembrar(
        engine_pg,
        vertical_code=rubro_scratch,
        margenes=_MARGENES_REALES,
        schema_version=EVENT_SCHEMA_VERSION,
    )

    async with _sesion_corta(engine_pg) as session:
        observado = await AnalyticsRepository(session).observed_margin_distribution(rubro_scratch)

    assert observado is not None, "seis eventos v2 alcanzan el mínimo de muestra"
    assert observado.event_count == len(_MARGENES_REALES), "las v1 no entran ni al conteo"
    assert observado.p10 >= min(_MARGENES_REALES), (
        f"p10={observado.p10}: un percentil por debajo del mínimo real solo puede "
        f"venir de que los ceros fabricados entraron a la muestra"
    )
    assert observado.p75 <= max(_MARGENES_REALES)


@pytest.mark.asyncio
async def test_solo_filas_viejas_no_produce_distribucion(
    engine_pg: AsyncEngine, rubro_scratch: str
) -> None:
    """Doce filas v1 superan el mínimo de muestra y aun así no hay distribución.

    Es el contrapeso al de arriba: prueba que el corte es por VERSIÓN y no por
    cantidad, y que ``get_distinct_verticals`` no deja al rubro asomando en la
    vista de administración con una fila vacía que parece "rubro sin actividad".
    """
    await _sembrar(
        engine_pg,
        vertical_code=rubro_scratch,
        margenes=[_MARGEN_FABRICADO] * 6 + _MARGENES_REALES,
        schema_version=1,
    )

    async with _sesion_corta(engine_pg) as session:
        repo = AnalyticsRepository(session)
        distribucion = await repo.observed_margin_distribution(rubro_scratch)
        verticales = await repo.get_distinct_verticals()

    assert distribucion is None
    assert rubro_scratch not in verticales


@pytest.mark.asyncio
async def test_las_filas_preexistentes_quedan_marcadas_como_viejas(
    engine_pg: AsyncEngine, rubro_scratch: str
) -> None:
    """El `server_default` de la columna es `1`, no la versión vigente.

    Es lo que cubre la ventana entre el preDeploy —donde corre la migración— y el
    reemplazo del proceso: en esos minutos el código VIEJO sigue insertando sin
    mandar la columna, y esas filas tienen que nacer marcadas como viejas. Un
    default de base igual a la versión vigente las dejaría pasar por nuevas, que
    es el bug que el corte por fecha tenía y este mecanismo vino a sacar.
    """
    async with _sesion_corta(engine_pg) as session:
        await session.execute(
            text(
                "INSERT INTO analytics_events "
                "(id, vertical_code, score_total, margin_ratio, created_at) "
                "VALUES (:id, :vc, 70, :mr, :now)"
            ),
            {
                "id": uuid.uuid4(),
                "vc": rubro_scratch,
                "mr": _MARGEN_FABRICADO,
                "now": datetime.now(UTC),
            },
        )

    async with _sesion_corta(engine_pg) as session:
        version = await session.scalar(
            text("SELECT schema_version FROM analytics_events WHERE vertical_code = :vc"),
            {"vc": rubro_scratch},
        )

    assert version == 1, "un escritor que no se pronuncia se asume viejo"
