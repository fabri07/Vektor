"""E6b — qué queda guardado cuando ``apply_reread`` falla a mitad.

Lo que este módulo afirma NO es que hoy exista corrupción. El plan pedía "la
prueba dedicada de rollback de `apply_reread`, con fallas inyectadas", y lo que
faltaba era la MEDICIÓN: sin ella, ni "queda medio estado" ni "revierte todo" son
afirmaciones sostenidas. Estos tests inyectan el fallo en tres puntos donde ya se
escribió algo, dejan que la transacción se deshaga como en producción, y
**consultan desde una sesión nueva** qué quedó.

Por qué desde otra sesión, y por qué PostgreSQL
-----------------------------------------------
Preguntarle a la misma sesión que acaba de hacer rollback no prueba nada: la
identity map ya no tiene los objetos y la respuesta saldría de la nada. Lo que
importa es lo que ve una conexión distinta después, que es lo que va a ver el
próximo request. Y va contra Postgres real porque una garantía de atomicidad
sobre SQLite mide la emulación de transacciones de pysqlite, no la del motor de
producción (ver ``[[feedback_sqlite_masks_postgres]]``).

Los tres puntos de inyección están elegidos por lo que ya escribieron cuando
fallan, no por dónde es cómodo pinchar:

* **antes de la reconciliación** — el ``DataRepairRun`` del apply ya existe;
* **después de los voids** — las filas viejas ya se anularon y el reimport no
  llegó a reponerlas: es el estado más peligroso posible, el que dejaría al
  tenant sin sus gastos;
* **después de reconciliar todo** — gastos, productos, stock, movimientos y
  proveedor ya reescritos, y falla el sellado final del archivo.

Se compara un snapshot COMPLETO —gastos con su ``voided_at``, productos con
stock y costo, proveedores, movimientos de inventario, el estado de relectura del
archivo y los runs— antes y después. Un rollback que revierte los gastos pero
deja el stock movido no lo detectaría una aserción sobre una sola tabla.

Gating: se skippea limpio sin ``TEST_PG_DSN``. Para correrlo::

    TEST_PG_DSN='postgresql+asyncpg://vektor:vektor@localhost:5432/vektor_pgtest' \\
        pytest app/tests/integration/test_reread_apply_rollback_pg.py -v --no-cov -n 0
"""

from __future__ import annotations

import inspect
import os
import uuid
from collections.abc import AsyncGenerator
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.application.services import reread_service
from app.tests.integration import _reread_scenario as esc

TEST_PG_DSN = os.environ.get("TEST_PG_DSN")

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not TEST_PG_DSN, reason="requiere TEST_PG_DSN (Postgres real)"),
]

#: Namespace propio para serializar el DDL entre workers de xdist.
_DDL_ADVISORY_KEY = 0x5645_4B54_4F52_0F6B  # "VEKTOR" + E6b

#: Una sola compra alcanza para este escenario: lo que se mide es la atomicidad
#: de la transacción, no la reconciliación de varias filas (eso lo mide
#: ``test_reread_row_reorder_pg``).
_FILAS = [esc.fila(5, esc.PRODUCTO, "5", "6000")]
_FILAS_CORREGIDAS = [esc.fila(5, esc.PRODUCTO, "5", "9000")]


@pytest_asyncio.fixture(scope="module")
async def pg_engine() -> AsyncGenerator[AsyncEngine, None]:
    assert TEST_PG_DSN is not None
    engine = await esc.crear_engine(TEST_PG_DSN, _DDL_ADVISORY_KEY)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def tenant_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest_asyncio.fixture
async def sm(
    pg_engine: AsyncEngine, tenant_id: uuid.UUID
) -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    factory = async_sessionmaker(pg_engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        await esc.limpiar(factory, tenant_id)


async def _apply_con_fallo(
    factory: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    file_id: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
    *,
    romper: str,
) -> None:
    """Corre el apply con un fallo inyectado y deja que la transacción se deshaga.

    El ``rollback`` explícito es lo que hace el caller real: ``get_db_session``
    ante una excepción, y el worker de relectura en su ``except``. Sin él, el
    cierre del ``async with`` también revertiría — pero entonces el test estaría
    probando el contextmanager de SQLAlchemy y no el camino de producción.

    El sustituto respeta si el original era corrutina o no: ``build_reread_summary``
    es sync y devolver una corrutina desde ahí no levantaría nada, dejaría un
    objeto raro asignado a ``file.reread_summary`` y el test pasaría por el motivo
    equivocado.
    """
    original = getattr(reread_service, romper)
    es_async = inspect.iscoroutinefunction(original)

    def _explota_sync(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError(f"fallo inyectado en {romper}")

    async def _explota_async(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError(f"fallo inyectado en {romper}")

    monkeypatch.setattr(reread_service, romper, _explota_async if es_async else _explota_sync)

    async with factory() as s:
        with pytest.raises(RuntimeError, match="fallo inyectado"):
            await reread_service.apply_reread(
                s, file_id, tenant_id, fresh_override=esc.summary(_FILAS_CORREGIDAS)
            )
        await s.rollback()


async def test_el_apply_completo_si_cambia_el_estado(
    sm: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> None:
    """Control del experimento, y el test que hace válidos a los otros.

    Si un apply exitoso no cambiara nada, "después del fallo quedó igual que
    antes" se cumpliría solo y los tests de abajo pasarían incluso con la
    transacción rota. Acá se comprueba que este escenario SÍ produce cambios
    visibles cuando termina bien.
    """
    file_id = await esc.importar_la_primera_vez(sm, tenant_id, _FILAS)
    antes = await esc.estado(sm, tenant_id)

    async with sm() as s:
        await reread_service.apply_reread(
            s, file_id, tenant_id, fresh_override=esc.summary(_FILAS_CORREGIDAS)
        )
        await s.commit()

    despues = await esc.estado(sm, tenant_id)
    assert despues != antes, "el escenario no cambia nada: los otros tests no probarían nada"


#: Punto de inyección → qué se escribió ANTES de que explote. El tercero es el
#: peligroso: corre la reconciliación entera (void + reimport) y falla después.
_PUNTOS = [
    ("_reread_master_entities", "el run del apply ya existe"),
    ("insert_confirmed_data", "los gastos viejos ya se anularon y el reimport no los repuso"),
    ("build_reread_summary", "gastos, productos, stock, movimientos y proveedor reescritos"),
]


@pytest.mark.parametrize(("romper", "que_ya_se_escribio"), _PUNTOS, ids=[p[0] for p in _PUNTOS])
async def test_un_fallo_a_mitad_no_deja_medio_estado(
    sm: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
    romper: str,
    que_ya_se_escribio: str,
) -> None:
    """El estado después del rollback tiene que ser IDÉNTICO al de antes."""
    file_id = await esc.importar_la_primera_vez(sm, tenant_id, _FILAS)
    antes = await esc.estado(sm, tenant_id)

    await _apply_con_fallo(sm, tenant_id, file_id, monkeypatch, romper=romper)

    despues = await esc.estado(sm, tenant_id)
    assert despues == antes, (
        f"fallo inyectado en {romper} ({que_ya_se_escribio}) dejó estado persistido.\n"
        f"antes:   {antes}\ndespués: {despues}"
    )
    # Aserción explícita del peor caso, además de la comparación completa: un
    # gasto anulado sin su reemplazo deja al tenant sin esa compra.
    assert not [g for g in despues["gastos"] if g[2]], (
        f"quedaron gastos anulados sin su reemplazo: {despues['gastos']}"
    )
