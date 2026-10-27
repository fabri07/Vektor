"""Las sentencias de identidad de operación van en tandas (I6-2).

Revisión profunda 2026-10-24. asyncpg corta una sentencia en 32.767 parámetros.
``_reclamar`` y ``_persistir_vinculos`` armaban UN ``INSERT`` multi-VALUES de 6
parámetros por fila (más de 5.461 comprobantes → el confirm entero fallaba);
``liberar_identidades_de`` un ``OR`` de 2 parámetros por efecto y ``vinculos_de``
un ``IN`` de todos los ids (más de ~16k ventas y gastos → no se podía borrar ni
releer el archivo, aunque no tuviera columnas de identidad: el borrado la llama
siempre). Las lecturas por clave ya iban en tandas (#203); ahora todo.

Se ejercita cada sentencia con 33.000 claves y efectos y se mide, en el cursor,
cuántos parámetros lleva cada sentencia: ninguna pasa el tope. La misma
corrida contra Postgres real (asyncpg) está en
``integration/test_identidad_en_tandas_i6_2_pg.py``.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services import operation_identity_service as ois
from app.persistence.models.operation_identity import OperationIdentity, OperationIdentityLink
from app.persistence.models.tenant import Tenant

#: El tope de asyncpg (y de SQLite ≥ 3.32 es 32.766).
TOPE_DE_PARAMETROS = 32_767
#: Más que el tope por sí solo: un ``IN`` de un parámetro por id ya lo pasa.
N = 33_000


@contextmanager
def _parametros_por_sentencia(session: AsyncSession) -> Iterator[list[int]]:
    """Cuántos parámetros lleva cada sentencia que llega al cursor."""
    engine = session.sync_session.get_bind()
    medidos: list[int] = []

    def _medir(
        _conn: Any, _cursor: Any, _sql: str, params: Any, _ctx: Any, executemany: bool
    ) -> None:
        if executemany:
            medidos.extend(len(p) for p in params)
        else:
            medidos.append(len(params or ()))

    event.listen(engine, "before_cursor_execute", _medir)
    try:
        yield medidos
    finally:
        event.remove(engine, "before_cursor_execute", _medir)


async def ejercitar_todo(session: AsyncSession, tenant_id: uuid.UUID) -> list[int]:
    """Reclamar, vincular, leer la foto, liberar y devolver reclamos, con N.

    Compartida con el test de Postgres. Devuelve los parámetros por sentencia."""
    with _parametros_por_sentencia(session) as medidos:
        contenidos = {f"k1:test:{i}": f"h{i}" for i in range(N)}
        ganadas, existentes = await ois._reclamar(
            session, tenant_id, None, contenidos=contenidos, entity="sale"
        )
        assert len(ganadas) == N
        assert len(existentes) == N

        efectos = [("sale", uuid.uuid4()) for _ in range(N)]
        await ois._persistir_vinculos(
            session,
            tenant_id,
            None,
            [(existentes[f"k1:test:{i}"][0], tipo, eid) for i, (tipo, eid) in enumerate(efectos)],
        )
        foto = await ois.vinculos_de(session, tenant_id, efectos)
        assert len(foto) == N

        assert await ois.liberar_identidades_de(session, tenant_id, efectos) == N
        assert (
            await session.scalar(
                select(func.count())
                .select_from(OperationIdentityLink)
                .where(OperationIdentityLink.tenant_id == tenant_id)
            )
            == 0
        )

        # Reclamos que no produjeron nada: el cierre del import los devuelve.
        sobrantes = {f"k1:sobra:{i}": f"h{i}" for i in range(N)}
        ganadas, existentes = await ois._reclamar(
            session, tenant_id, None, contenidos=sobrantes, entity="expense"
        )
        plan = ois.plan_vacio()
        plan.reclamadas = ganadas
        plan.identity_id = {clave: ident for clave, (ident, _, _) in existentes.items()}
        await ois.cerrar_plan(session, tenant_id, None, [plan])
        assert (
            await session.scalar(
                select(func.count())
                .select_from(OperationIdentity)
                .where(OperationIdentity.tenant_id == tenant_id)
            )
            == 0
        )
    return medidos


async def test_ninguna_sentencia_de_identidad_pasa_el_tope_de_parametros(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    medidos = await ejercitar_todo(db_session, sample_tenant.tenant_id)

    assert medidos, "el listener no midió nada"
    assert max(medidos) <= TOPE_DE_PARAMETROS, max(medidos)


async def test_liberar_sin_vinculos_corta_con_una_sola_lectura_por_tanda(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Un archivo sin columnas de identidad (el borrado igual llama): ninguna
    escritura, sólo las lecturas por tanda."""
    efectos = [("sale", uuid.uuid4()) for _ in range(N)]
    with _parametros_por_sentencia(db_session) as medidos:
        assert await ois.liberar_identidades_de(db_session, sample_tenant.tenant_id, efectos) == 0
    # Sin contar el BEGIN de la transacción (0 parámetros).
    assert len([m for m in medidos if m]) == -(-N // ois._IDS_POR_SENTENCIA)
    assert max(medidos) <= TOPE_DE_PARAMETROS


def test_las_tandas_no_pierden_ni_repiten_nada() -> None:
    datos = list(range(12_345))
    tandas = ois.en_tandas(datos, 1000)
    assert [x for t in tandas for x in t] == datos
    assert max(len(t) for t in tandas) == 1000
    assert ois.en_tandas([], 1000) == []
    # 6 parámetros por fila de INSERT: la tanda no pasa el tope.
    assert ois._FILAS_POR_INSERT * 6 < TOPE_DE_PARAMETROS
    assert ois._IDS_POR_SENTENCIA + 2 < TOPE_DE_PARAMETROS
