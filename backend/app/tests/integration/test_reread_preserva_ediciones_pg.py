"""E6b — «editar manualmente y releer preserva esa edición».

Es un criterio de aceptación del plan (F6), y la preservación depende de un solo
dato: ``has_user_edits``. ``_split_records`` parte los registros en editados —que
se preservan— y no editados —que se anulan y se reimportan—, así que cualquier
camino que deje editar un registro importado SIN marcar el flag hace que la
relectura descarte ese trabajo en silencio.

El defecto que motivó este módulo, medido antes de corregirlo: ``PATCH
/expenses/{id}`` marcaba el flag, pero ``ExpenseRepository.reclassify`` —el camino
de **reclasificar un gasto por chat** (``RECLASSIFY_EXPENSE``)— no. La medición
sobre Postgres real dio:

* import        → ``category=OTHER``, ``expense_type=COGS``
* reclasificado → ``category=SUPPLIES``, ``expense_type=OPEX``, flag en ``False``
* tras releer   → el reclasificado ANULADO y uno nuevo con ``OTHER/COGS``

O sea: la decisión contable del usuario desaparecía y volvía la del parser.

Va contra Postgres real porque lo que se afirma es el estado persistido después
de una relectura completa, con su void + reimport — no el retorno de una
función.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.application.services import reread_service
from app.persistence.models.transaction import ExpenseEntry
from app.persistence.repositories.transaction_repository import ExpenseRepository
from app.tests.integration import _reread_scenario as esc

TEST_PG_DSN = os.environ.get("TEST_PG_DSN")

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not TEST_PG_DSN, reason="requiere TEST_PG_DSN (Postgres real)"),
]

_DDL_ADVISORY_KEY = 0x5645_4B54_4F52_0F6E  # "VEKTOR" + E6b (ediciones)

_FILAS = [esc.fila(5, esc.PRODUCTO, "5", "6000")]

#: Índices dentro de la tupla de gasto que arma ``esc.estado``.
_ANULADO, _CATEGORIA, _TIPO, _EDITADO = 2, 4, 5, 6


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


async def _reclasificar_como_por_chat(
    factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> None:
    """El mismo camino que ejecuta ``RECLASSIFY_EXPENSE`` al confirmarse.

    Se llama al repositorio y no al endpoint HTTP a propósito: la acción del chat
    entra por ``pending_action_service._execute_reclassify_expense``, que usa este
    repositorio. Probar el PATCH no cubriría este camino, y era justo el que
    faltaba marcar.
    """
    async with factory() as s:
        entry = (
            (await s.execute(select(ExpenseEntry).where(ExpenseEntry.tenant_id == tenant_id)))
            .scalars()
            .first()
        )
        assert entry is not None
        await ExpenseRepository(s).reclassify(entry, category="SUPPLIES", expense_type="OPEX")
        await s.commit()


async def test_una_reclasificacion_por_chat_sobrevive_a_la_relectura(
    sm: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> None:
    file_id = await esc.importar_la_primera_vez(sm, tenant_id, _FILAS)
    await _reclasificar_como_por_chat(sm, tenant_id)

    tras_editar = await esc.estado(sm, tenant_id)
    assert tras_editar["gastos"][0][_EDITADO] is True, (
        "reclasificar no marcó el gasto como editado: la relectura lo va a pisar"
    )

    async with sm() as s:
        await reread_service.apply_reread(
            s, file_id, tenant_id, fresh_override=esc.summary(_FILAS)
        )
        await s.commit()

    vivos = esc.gastos_vivos(await esc.estado(sm, tenant_id))
    assert len(vivos) == 1, f"la relectura duplicó el gasto reclasificado: {vivos}"
    assert vivos[0][_CATEGORIA] == "SUPPLIES", (
        f"volvió la clasificación del parser y se perdió la del usuario: {vivos[0]}"
    )
    assert vivos[0][_TIPO] == "OPEX", vivos[0]


async def test_un_gasto_no_editado_sigue_reimportandose(
    sm: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> None:
    """La contracara, que es lo que hace peligroso el arreglo.

    Si marcar el flag se le escapara a un gasto que el usuario NO tocó, la
    relectura dejaría de poder corregirlo — y corregir lo mal interpretado es
    exactamente para lo que existe. Sin este caso, "preservar" podría convertirse
    en "no volver a leer nunca".
    """
    file_id = await esc.importar_la_primera_vez(sm, tenant_id, _FILAS)
    antes = await esc.estado(sm, tenant_id)
    assert antes["gastos"][0][_EDITADO] is False

    corregido = [esc.fila(5, esc.PRODUCTO, "5", "9000")]
    async with sm() as s:
        await reread_service.apply_reread(
            s, file_id, tenant_id, fresh_override=esc.summary(corregido)
        )
        await s.commit()

    vivos = esc.gastos_vivos(await esc.estado(sm, tenant_id))
    assert [g[1] for g in vivos] == ["9000.00"], (
        f"la relectura no corrigió un gasto que nadie había editado: {vivos}"
    )


async def test_la_edicion_preservada_no_deja_el_gasto_duplicado(
    sm: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> None:
    """Preservar la fila editada y reimportar la del archivo dejaría DOS gastos
    por la misma compra: la plata contada dos veces. La reconciliación usa el
    ``source_row_ref`` del editado para saltear su fila en el reimport, y esto lo
    afirma sobre el total vivo, que es lo que ve el dueño del negocio."""
    file_id = await esc.importar_la_primera_vez(sm, tenant_id, _FILAS)
    await _reclasificar_como_por_chat(sm, tenant_id)

    async with sm() as s:
        await reread_service.apply_reread(
            s, file_id, tenant_id, fresh_override=esc.summary(_FILAS)
        )
        await s.commit()

    final = await esc.estado(sm, tenant_id)
    vivos = esc.gastos_vivos(final)
    assert [g[1] for g in vivos] == ["6000.00"], f"la compra se contó dos veces: {vivos}"
    anulados = [g for g in final["gastos"] if g[_ANULADO]]
    assert not anulados, f"se anuló algo que había que preservar: {anulados}"


def test_los_indices_del_snapshot_son_los_que_se_creen() -> None:
    """Las aserciones de arriba indexan la tupla por posición. Si ``esc.estado``
    cambiara de forma, seguirían pasando midiendo otra cosa — este test las ata a
    la forma real."""
    from inspect import getsource

    fuente = getsource(esc.estado)
    orden = ["str(g.id)", "str(g.amount)", "g.voided_at", "str(g.product_id)"]
    orden += ["g.category", "g.expense_type", "g.has_user_edits"]
    posiciones = [fuente.index(campo) for campo in orden]
    assert posiciones == sorted(posiciones), "cambió el orden de la tupla de gastos"
    assert (_ANULADO, _CATEGORIA, _TIPO, _EDITADO) == (2, 4, 5, 6)
