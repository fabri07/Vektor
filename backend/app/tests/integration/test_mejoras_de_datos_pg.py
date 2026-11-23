"""«Mejoras de datos» contra Postgres real: lo que SQLite no ejercita.

SQLite no tiene advisory locks ni ``FOR UPDATE``: la garantía de que dos tareas
simultáneas sobre el mismo tenant no apliquen dos veces la misma mejora sólo se
prueba acá. Cada tarea usa su propia conexión (``NullPool``): los advisory locks
son por conexión.

    TEST_PG_DSN=... DATABASE_URL=... pytest -m postgres -n 0 \\
        app/tests/integration/test_mejoras_de_datos_pg.py --no-cov
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncGenerator, Iterator
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.application.services.mejoras_de_datos import servicio
from app.application.services.mejoras_de_datos.compra_sin_costo import CompraSinCosto
from app.jobs.mejoras_de_datos import procesar
from app.persistence.models.audit import DecisionAuditLog
from app.persistence.models.product import Product
from app.persistence.models.repair import DataRepairRun
from app.persistence.models.tenant import Tenant
from app.tests._mejora_juguete import VALOR, MejoraJuguete, registro_con

TEST_PG_DSN = os.environ.get("TEST_PG_DSN")

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.usefixtures("mock_score_trigger"),
    pytest.mark.skipif(not TEST_PG_DSN, reason="requiere PostgreSQL real (TEST_PG_DSN)"),
]


@pytest.fixture(autouse=True)
def _registro() -> Iterator[None]:
    with registro_con(MejoraJuguete(), CompraSinCosto()):
        yield


@pytest_asyncio.fixture(scope="module")
async def pg_engine() -> AsyncGenerator[AsyncEngine, None]:
    assert TEST_PG_DSN is not None
    engine = create_async_engine(TEST_PG_DSN, poolclass=NullPool)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def sm(
    pg_engine: AsyncEngine,
) -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    yield async_sessionmaker(pg_engine, expire_on_commit=False)


@pytest_asyncio.fixture
async def tenant_id(sm: async_sessionmaker[AsyncSession]) -> AsyncGenerator[uuid.UUID, None]:
    tid = uuid.uuid4()
    async with sm() as s:
        s.add(Tenant(tenant_id=tid, legal_name="T", display_name="T", status="ACTIVE"))
        await s.flush()
        for i in range(30):
            s.add(
                Product(
                    id=uuid.uuid4(),
                    tenant_id=tid,
                    name=f"P{i}",
                    sale_price_ars=Decimal("100"),
                    stock_units=0,
                )
            )
        await s.commit()
    try:
        yield tid
    finally:
        async with sm() as s:
            for modelo in (DecisionAuditLog, DataRepairRun, Product):
                await s.execute(delete(modelo).where(modelo.tenant_id == tid))
            await s.execute(delete(Tenant).where(Tenant.tenant_id == tid))
            await s.commit()


async def _cuenta(sm: async_sessionmaker[AsyncSession], modelo: Any, *cond: Any) -> int:
    async with sm() as s:
        return int(
            await s.scalar(select(func.count()).select_from(modelo).where(*cond)) or 0
        )


async def test_dos_tareas_simultaneas_aplican_una_sola_vez(
    sm: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> None:
    """Dos ``procesar`` a la vez sobre el mismo tenant: cada producto se completa y
    se audita UNA vez (no 2×30)."""
    r1, r2 = await asyncio.gather(procesar(sm, tenant_id), procesar(sm, tenant_id))

    assert r1["juguete"] + r2["juguete"] == 30
    assert (
        await _cuenta(
            sm,
            DecisionAuditLog,
            DecisionAuditLog.tenant_id == tenant_id,
            DecisionAuditLog.decision_type == "MEJORA_DE_DATOS:juguete",
        )
        == 30
    )
    assert (
        await _cuenta(
            sm, Product, Product.tenant_id == tenant_id, Product.description == VALOR
        )
        == 30
    )
    # Una tercera pasada ya no encuentra nada (el detector es el libro de cuentas).
    assert (await procesar(sm, tenant_id))["juguete"] == 0


async def test_compra_sin_costo_contra_postgres(
    sm: async_sessionmaker[AsyncSession], pg_engine: AsyncEngine
) -> None:
    """El SQL de ``compra_sin_costo`` (``FOR UPDATE OF`` con join, agrupados, JSON del
    confirm) corre en Postgres: SQLite ignora el ``FOR UPDATE`` y no lo prueba."""
    from datetime import UTC, datetime  # noqa: PLC0415

    from app.persistence.models.file import UploadedFile  # noqa: PLC0415
    from app.persistence.models.inventory import InventoryMovement  # noqa: PLC0415
    from app.persistence.models.pipeline_event import PipelineEvent  # noqa: PLC0415
    from app.persistence.models.transaction import ExpenseEntry  # noqa: PLC0415

    tid, upload, pid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with sm() as s:
        s.add(Tenant(tenant_id=tid, legal_name="T", display_name="T", status="ACTIVE"))
        await s.flush()
        s.add(
            UploadedFile(
                id=upload, tenant_id=tid, original_filename="a.xlsx", s3_key="k",
                content_type="x", size_bytes=1, purpose="general",
            )
        )
        s.add(
            Product(
                id=pid, tenant_id=tid, name="Jeans", sale_price_ars=Decimal("100"), stock_units=3
            )
        )
        await s.flush()
        s.add(
            InventoryMovement(
                tenant_id=tid, product_id=pid, movement_type="purchase", qty=3,
                source_type="purchase_import", source_upload_id=upload,
                source_row_ref="r1", cost_contract_version=1,
                occurred_at=datetime(2026, 9, 1, 10, tzinfo=UTC),
            )
        )
        s.add(
            ExpenseEntry(
                tenant_id=tid, amount=Decimal("4500.00"), category="INVENTORY",
                expense_type="COGS", transaction_date=datetime(2026, 9, 1, 10),
                description="c", product_id=pid, source_upload_id=upload,
                source_row_ref="r1",
            )
        )
        s.add(
            PipelineEvent(
                trace_id=uuid.uuid4(), tenant_id=tid, file_id=upload, stage="confirm",
                detail={"mappings": {"flat": {"Costo total ARS": "amount"}}},
            )
        )
        await s.commit()
    try:
        async with sm() as s:
            det = await CompraSinCosto().detectar(s, tid, bloquear=True)
        assert [c.detalle["unit_cost"] for c in det.candidatos] == ["1500.00"]

        async with sm() as s:
            r = await servicio.aplicar(s, tid, "compra_sin_costo", automatica=True)
            await s.commit()
        assert r.aplicadas == 1
        async with sm() as s:
            m = (
                await s.execute(
                    select(InventoryMovement).where(InventoryMovement.tenant_id == tid)
                )
            ).scalar_one()
            assert (m.unit_cost, m.unit_cost_final) == (Decimal("1500.00"), Decimal("1500.00"))
            otra = await servicio.aplicar(s, tid, "compra_sin_costo", automatica=True)
            assert otra.aplicadas == 0
    finally:
        from app.persistence.models.repair import DataRepairItem  # noqa: PLC0415

        async with sm() as s:
            for modelo in (
                DecisionAuditLog, DataRepairItem, DataRepairRun, PipelineEvent,
                ExpenseEntry, InventoryMovement, Product, UploadedFile,
            ):
                await s.execute(delete(modelo).where(modelo.tenant_id == tid))
            await s.execute(delete(Tenant).where(Tenant.tenant_id == tid))
            await s.commit()


async def test_compra_catalogo_sin_costo_contra_postgres(
    sm: async_sessionmaker[AsyncSession],
) -> None:
    """``cambia_valores`` contra Postgres real: lock EXCLUSIVO con ``lock_timeout``, el
    ``FOR UPDATE OF`` con join, el gasto nuevo y el Deshacer que lo anula. La evidencia
    del archivo (S3) se pasa ya resuelta: acá se prueba la base, no la lectura."""
    from datetime import UTC, datetime  # noqa: PLC0415

    from app.application.services.mejoras_de_datos.compra_catalogo_sin_costo import (  # noqa: PLC0415
        CompraCatalogoSinCosto,
        Evidencia,
    )
    from app.persistence.models.file import UploadedFile  # noqa: PLC0415
    from app.persistence.models.inventory import InventoryMovement  # noqa: PLC0415
    from app.persistence.models.pipeline_event import PipelineEvent  # noqa: PLC0415
    from app.persistence.models.repair import DataRepairItem  # noqa: PLC0415
    from app.persistence.models.transaction import ExpenseEntry  # noqa: PLC0415

    tid, upload, pid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    mejora = CompraCatalogoSinCosto()
    async with sm() as s:
        s.add(Tenant(tenant_id=tid, legal_name="T", display_name="T", status="ACTIVE"))
        await s.flush()
        s.add(
            UploadedFile(
                id=upload, tenant_id=tid, original_filename="a.xlsx", s3_key="k",
                content_type="x", size_bytes=1, purpose="stock",
            )
        )
        s.add(
            Product(
                id=pid, tenant_id=tid, name="Camisa", sale_price_ars=Decimal("100"), stock_units=8
            )
        )
        await s.flush()
        s.add(
            InventoryMovement(
                tenant_id=tid, product_id=pid, movement_type="purchase", qty=8,
                source_type="catalog_initial_stock", source_upload_id=upload,
                source_row_ref="r1", occurred_at=datetime(2026, 11, 14, 10, tzinfo=UTC),
            )
        )
        s.add(
            PipelineEvent(
                trace_id=uuid.uuid4(), tenant_id=tid, file_id=upload, stage="confirm",
                detail={
                    "mappings": {
                        "flat": {},
                        "context": {"sheet:t": {"Costo total": "purchase_base_cost"}},
                    },
                    "stock_treatment": {"sheet:t": "purchase"},
                },
            )
        )
        await s.commit()
    try:
        async with sm() as s:
            det = await mejora.detectar(s, tid, bloquear=True)
        assert [c.etiqueta for c in det.candidatos] == ["Camisa"]
        evidencia = {
            det.candidatos[0].id: Evidencia(
                total=Decimal("48000"), unit_cost=Decimal("6000.00"),
                payment_method="transfer", medio_por_defecto=True, hoja="sheet:t",
            )
        }
        with registro_con(mejora):
            async with sm() as s:
                r = await servicio.aplicar(
                    s, tid, "compra_catalogo_sin_costo", evidencia=evidencia
                )
                await s.commit()
            assert r.aplicadas == 1 and r.run_id is not None
            async with sm() as s:
                gasto = (
                    await s.execute(select(ExpenseEntry).where(ExpenseEntry.tenant_id == tid))
                ).scalar_one()
                assert (gasto.amount, gasto.payment_method) == (Decimal("48000.00"), "transfer")
                assert gasto.voided_at is None
            async with sm() as s:
                rev = await servicio.revertir(s, tid, r.run_id)
                await s.commit()
            assert rev.completa
            async with sm() as s:
                gasto = (
                    await s.execute(select(ExpenseEntry).where(ExpenseEntry.tenant_id == tid))
                ).scalar_one()
                assert gasto.void_reason == "MANUAL_ADMIN_VOID"
                m = (
                    await s.execute(
                        select(InventoryMovement).where(InventoryMovement.tenant_id == tid)
                    )
                ).scalar_one()
                assert m.unit_cost is None
    finally:
        async with sm() as s:
            for modelo in (
                DecisionAuditLog, DataRepairItem, DataRepairRun, PipelineEvent,
                ExpenseEntry, InventoryMovement, Product, UploadedFile,
            ):
                await s.execute(delete(modelo).where(modelo.tenant_id == tid))
            await s.execute(delete(Tenant).where(Tenant.tenant_id == tid))
            await s.commit()
