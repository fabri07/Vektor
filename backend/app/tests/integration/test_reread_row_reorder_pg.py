"""E6b — ¿qué pasa cuando el mismo archivo vuelve con las filas en otro orden?

La pregunta viene del plan, que la deja planteada como límite conocido: «la
posición física identifica la fila dentro de una versión del original; no alcanza
para vincular filas entre versiones reordenadas». La huella de idempotencia del
import es ``sha256(tenant:IMPORT_ROW:file_id:context_id:row_index)`` — el
``row_index`` es la posición—, así que una fila que se mueve de lugar cambia de
huella y, para la dedup, es otra fila.

Esto **mide** qué produce esa mecánica. No asume el defecto: una relectura anula
lo no editado y reimporta, así que el resultado neto podría ser correcto incluso
con las huellas corridas. Lo que se afirma es lo observable por el dueño del
negocio: cuántos gastos VIVOS quedan y por cuánta plata.

Escenario mínimo pero no trivial: tres compras del mismo proveedor con importes
distintos. Reordenarlas cambia la posición de dos de las tres, y los importes
distintos permiten ver si alguna se duplicó o desapareció — con tres filas
idénticas, cualquier resultado sumaría lo mismo y el test no distinguiría nada.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncGenerator
from decimal import Decimal
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

_DDL_ADVISORY_KEY = 0x5645_4B54_4F52_0F6C  # "VEKTOR" + E6b (reorder)

#: Tres compras con importes distintos, para poder decir cuál se duplicó.
_FILAS = [
    esc.fila(5, esc.PRODUCTO, "5", "6000"),
    esc.fila(6, esc.PRODUCTO, "3", "3600"),
    esc.fila(7, esc.PRODUCTO, "2", "2400"),
]
#: El MISMO contenido, en otro orden. Nada cambió del negocio: sólo la posición.
_FILAS_REORDENADAS = [_FILAS[2], _FILAS[0], _FILAS[1]]

_TOTAL_ESPERADO = Decimal("12000")  # 6000 + 3600 + 2400


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


def _total_vivo(snapshot: dict[str, Any]) -> Decimal:
    return sum((Decimal(g[1]) for g in esc.gastos_vivos(snapshot)), Decimal("0"))


async def test_releer_el_mismo_archivo_sin_cambios_no_duplica(
    sm: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> None:
    """Control: la relectura idempotente. Sin esto, el test del reorden no
    distinguiría "reordenar rompe" de "releer rompe"."""
    file_id = await esc.importar_la_primera_vez(sm, tenant_id, _FILAS)
    assert _total_vivo(await esc.estado(sm, tenant_id)) == _TOTAL_ESPERADO

    async with sm() as s:
        await reread_service.apply_reread(
            s, file_id, tenant_id, fresh_override=esc.summary(_FILAS)
        )
        await s.commit()

    final = await esc.estado(sm, tenant_id)
    assert _total_vivo(final) == _TOTAL_ESPERADO, (
        f"releer el mismo archivo cambió la plata viva: {esc.gastos_vivos(final)}"
    )
    assert len(esc.gastos_vivos(final)) == len(_FILAS)


async def test_releer_el_mismo_archivo_reordenado_no_duplica_ni_pierde(
    sm: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> None:
    """Las mismas tres compras, en otro orden: la plata viva no puede cambiar.

    Es el caso real de un usuario que ordena la planilla por importe o por fecha
    antes de volver a subirla. Nada del negocio cambió; si el resultado cambia,
    cambió por la posición de las filas, que no es un hecho económico.
    """
    file_id = await esc.importar_la_primera_vez(sm, tenant_id, _FILAS)
    antes = await esc.estado(sm, tenant_id)

    async with sm() as s:
        await reread_service.apply_reread(
            s, file_id, tenant_id, fresh_override=esc.summary(_FILAS_REORDENADAS)
        )
        await s.commit()

    despues = await esc.estado(sm, tenant_id)
    vivos = esc.gastos_vivos(despues)
    assert _total_vivo(despues) == _TOTAL_ESPERADO, (
        f"reordenar las filas cambió la plata viva: {_total_vivo(antes)} → "
        f"{_total_vivo(despues)}\nvivos: {vivos}"
    )
    assert len(vivos) == len(_FILAS), f"quedaron {len(vivos)} gastos vivos y son 3 compras: {vivos}"
    assert sorted(Decimal(g[1]) for g in vivos) == [
        Decimal("2400"),
        Decimal("3600"),
        Decimal("6000"),
    ], f"los importes vivos no son los del archivo: {vivos}"


async def test_el_stock_del_producto_no_se_duplica_al_reordenar(
    sm: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> None:
    """La otra mitad del mismo riesgo: el stock lo mueven los movimientos de
    inventario, que se anclan igual. Diez unidades compradas siguen siendo diez
    después de reordenar el archivo."""
    file_id = await esc.importar_la_primera_vez(sm, tenant_id, _FILAS)
    antes = await esc.estado(sm, tenant_id)
    stock_antes = [p[1] for p in antes["productos"]]

    async with sm() as s:
        await reread_service.apply_reread(
            s, file_id, tenant_id, fresh_override=esc.summary(_FILAS_REORDENADAS)
        )
        await s.commit()

    despues = await esc.estado(sm, tenant_id)
    assert [p[1] for p in despues["productos"]] == stock_antes, (
        f"el stock cambió al reordenar: {antes['productos']} → {despues['productos']}"
    )
