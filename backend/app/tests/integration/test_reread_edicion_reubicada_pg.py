"""B·nuevo contra PostgreSQL real: la edición reubicada, su stock, sus huellas y
el undo.

La suite SQLite ya lo prueba; acá se repite donde SQLite no mide: la unicidad de
``operation_fingerprints`` (una huella registrada dos veces sería un
``IntegrityError``), la columna nueva creada por la migración y no por
``create_all``, y la reversa incremental del stock.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncGenerator
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.application.services import reread_service
from app.persistence.models.inventory import InventoryMovement
from app.persistence.models.memory import OperationFingerprint
from app.persistence.models.transaction import ExpenseEntry
from app.tests.integration import _reread_scenario as esc

TEST_PG_DSN = os.environ.get("TEST_PG_DSN")

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not TEST_PG_DSN, reason="requiere TEST_PG_DSN (Postgres real)"),
]

_DDL_ADVISORY_KEY = 0x5645_4B54_4F52_0F70  # "VEKTOR" + B·nuevo

_FILAS = [
    esc.fila(5, "Vela aromatica 200g", "5", "6000"),
    esc.fila(6, "Jabon de glicerina", "3", "3600"),
    esc.fila(7, "Difusor de bambu", "2", "2400"),
]
_NUEVA = esc.fila(4, "Sahumerio palo santo", "1", "1200")


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


def _ref(tenant_id: uuid.UUID, file_id: uuid.UUID, idx: int) -> str:
    return reread_service._hash_anchor(reread_service._row_anchor(tenant_id, file_id, None, idx))


async def _editar(
    factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID, monto: str
) -> uuid.UUID:
    async with factory() as s:
        rec = (
            await s.execute(
                select(ExpenseEntry).where(
                    ExpenseEntry.tenant_id == tenant_id, ExpenseEntry.amount == Decimal(monto)
                )
            )
        ).scalar_one()
        assert rec.source_row_digest, "el import escribe la huella de la fila original"
        rec.has_user_edits = True
        rec.description = "corregido a mano"
        await s.commit()
        return rec.id


async def _foto(
    factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID, file_id: uuid.UUID
) -> dict[str, Any]:
    async with factory() as s:
        anclas = {_ref(tenant_id, file_id, i) for i in range(10)}
        huellas = set(
            (
                await s.execute(
                    select(OperationFingerprint.fingerprint).where(
                        OperationFingerprint.tenant_id == tenant_id,
                        OperationFingerprint.fingerprint.in_(anclas),
                    )
                )
            ).scalars()
        )
        gastos = {
            (str(g.id), g.source_row_ref)
            for g in (
                await s.execute(
                    select(ExpenseEntry).where(
                        ExpenseEntry.tenant_id == tenant_id, ExpenseEntry.voided_at.is_(None)
                    )
                )
            ).scalars()
        }
        movs = {
            (str(m.id), m.source_row_ref, m.qty)
            for m in (
                await s.execute(
                    select(InventoryMovement).where(
                        InventoryMovement.tenant_id == tenant_id,
                        InventoryMovement.voided_at.is_(None),
                    )
                )
            ).scalars()
        }
    estado = await esc.estado(factory, tenant_id)
    return {
        "huellas": huellas,
        "gastos": gastos,
        "movimientos": movs,
        "stock": {p[0]: p[1] for p in estado["productos"] if p[1]},
    }


async def test_la_compra_editada_se_reubica_con_su_stock_y_el_undo_la_devuelve(
    sm: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> None:
    file_id = await esc.importar_la_primera_vez(sm, tenant_id, _FILAS)
    editado = await _editar(sm, tenant_id, "3600")
    antes = await _foto(sm, tenant_id, file_id)
    (mov_editado,) = [m for m in antes["movimientos"] if m[1] == _ref(tenant_id, file_id, 1)]

    async with sm() as s:
        resultado = await reread_service.apply_reread(
            s, file_id, tenant_id, fresh_override=esc.summary([_NUEVA, *_FILAS])
        )
        await s.commit()
    despues = await _foto(sm, tenant_id, file_id)

    assert [r["id"] for r in resultado.reubicaciones] == [str(editado)]
    assert (str(editado), _ref(tenant_id, file_id, 2)) in despues["gastos"]
    assert len(despues["gastos"]) == 4, "ni duplicada ni salteada"
    # El MISMO movimiento, con la ref nueva: su stock no se tocó.
    assert (mov_editado[0], _ref(tenant_id, file_id, 2), mov_editado[2]) in despues["movimientos"]
    assert despues["stock"] == {**antes["stock"], "Sahumerio palo santo": 1}
    assert despues["huellas"] == {ref for _id, ref in despues["gastos"]}

    async with sm() as s:
        await reread_service.undo_reread(s, resultado.run_id, tenant_id)
        await s.commit()
    revertido = await _foto(sm, tenant_id, file_id)
    assert revertido["gastos"] == antes["gastos"]
    assert revertido["movimientos"] == antes["movimientos"]
    assert revertido["stock"] == antes["stock"]
    # La de la fila 3 es de una fila que la relectura INSERTÓ: el undo nunca
    # suelta esas huellas (preexistente, fuera de este paso).
    assert revertido["huellas"] == antes["huellas"] | {_ref(tenant_id, file_id, 3)}


async def test_dos_relecturas_seguidas_con_corrimientos_distintos(
    sm: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> None:
    """Cadena e intercambio en la base real: cada huella se registra una sola vez
    (un duplicado sería un ``IntegrityError`` de la unicidad)."""
    file_id = await esc.importar_la_primera_vez(sm, tenant_id, _FILAS)
    a = await _editar(sm, tenant_id, "6000")
    b = await _editar(sm, tenant_id, "3600")

    for filas in ([_FILAS[1], _FILAS[0], _FILAS[2]], [_NUEVA, _FILAS[2], _FILAS[0], _FILAS[1]]):
        async with sm() as s:
            await reread_service.apply_reread(
                s, file_id, tenant_id, fresh_override=esc.summary(filas)
            )
            await s.commit()
        foto = await _foto(sm, tenant_id, file_id)
        assert len(foto["gastos"]) == len(filas)
        assert foto["huellas"] == {ref for _id, ref in foto["gastos"]}
        refs = dict(foto["gastos"])
        assert refs[str(a)] == _ref(tenant_id, file_id, filas.index(_FILAS[0]))
        assert refs[str(b)] == _ref(tenant_id, file_id, filas.index(_FILAS[1]))
