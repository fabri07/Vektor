"""I2-9 / I6-8 — «importar sugeridos» no entra en una fila que otra sesión está
resolviendo.

Con V-4 la venta del lote descuenta stock: si leyera como PENDING una fila que
``reclassify`` (o ``link-product``, o el descarte) tiene tomada con FOR UPDATE,
la venta y su descuento entrarían dos veces, o una fila descartada se importaría
igual. El lote lee los PENDING con ``FOR UPDATE SKIP LOCKED``: la fila tomada se
saltea (queda para la próxima corrida) y el lote no espera. Corre sólo contra
Postgres real: en SQLite FOR UPDATE es un no-op.

    TEST_PG_DSN='postgresql+asyncpg://vektor:vektor@localhost:5432/vektor_pgtest' \\
      pytest app/tests/integration/test_otros_lote_fila_tomada_pg.py -m postgres -n 0
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.application.services.ingestion_import_service import bulk_import_unclassified
from app.persistence.models.memory import OperationFingerprint
from app.persistence.models.transaction import ExpenseEntry
from app.persistence.models.unclassified_record import UnclassifiedRecord
from app.tests.integration import _reread_scenario as esc

TEST_PG_DSN = os.environ.get("TEST_PG_DSN")

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not TEST_PG_DSN, reason="requiere PostgreSQL real (TEST_PG_DSN)"),
]

_DDL_ADVISORY_KEY = 0x5645_4B54_4F52_1E4B  # el mismo que los demás tests de candados


@pytest_asyncio.fixture(scope="module")
async def pg_engine() -> AsyncGenerator[AsyncEngine, None]:
    assert TEST_PG_DSN is not None
    engine = await esc.crear_engine(TEST_PG_DSN, _DDL_ADVISORY_KEY)
    try:
        yield engine
    finally:
        await engine.dispose()


async def test_el_lote_saltea_la_fila_que_otra_sesion_esta_resolviendo(
    pg_engine: AsyncEngine,
) -> None:
    sm = async_sessionmaker(pg_engine, expire_on_commit=False, autoflush=False)
    tid = uuid.uuid4()
    file_id = await esc.preparar_tenant_y_archivo(sm, tid, [])
    try:
        async with sm() as s:
            fila = UnclassifiedRecord(
                tenant_id=tid,
                uploaded_file_id=file_id,
                source="ingestion",
                context_label="Fila sin cliente",
                suggested_entity="expense",
                status="PENDING",
                row_data={
                    "fecha": "2025-05-01", "detalle": "Pago luz", "monto": "15000",
                    "categoria": "Luz",
                },
            )
            s.add(fila)
            await s.commit()

        # La otra sesión (reclassify / dismiss) toma la fila y no commitea todavía.
        resolviendo = sm()
        tomada = (
            await resolviendo.execute(
                select(UnclassifiedRecord)
                .where(UnclassifiedRecord.id == fila.id)
                .with_for_update()
            )
        ).scalar_one()

        async def _lote() -> dict[str, int]:
            async with sm() as s:
                cuerpo = await bulk_import_unclassified(s, tid)
                await s.commit()
                return cuerpo

        try:
            cuerpo = await asyncio.wait_for(_lote(), 30)
        finally:
            tomada.status = "DISMISSED"
            await resolviendo.commit()
            await resolviendo.close()
        assert cuerpo["imported_expenses"] == 0, cuerpo

        async with sm() as s:
            assert (
                await s.execute(select(ExpenseEntry).where(ExpenseEntry.tenant_id == tid))
            ).scalars().all() == []
            assert (await s.get(UnclassifiedRecord, fila.id)).status == "DISMISSED"  # type: ignore[union-attr]
    finally:
        async with sm() as s:
            await s.execute(delete(UnclassifiedRecord).where(UnclassifiedRecord.tenant_id == tid))
            await s.execute(
                delete(OperationFingerprint).where(OperationFingerprint.tenant_id == tid)
            )
            await s.commit()
        await esc.limpiar(sm, tid)
