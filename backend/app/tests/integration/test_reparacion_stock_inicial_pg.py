"""M·R contra Postgres: lock exclusivo, ``FOR UPDATE`` y la elección en JSONB.

La suite SQLite no ve nada de esto: el lock es no-op, ``with_for_update`` se
ignora y el JSON no es JSONB. Acá se importa el catálogo como compra, se repara
y se revierte en transacciones reales, cada una con su commit.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncGenerator
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

import app.application.services.ingestion_import_service as importer
from app.application.services import reparacion_stock_inicial_service as mr
from app.domain.verticals import Vertical
from app.persistence.models.audit import DecisionAuditLog
from app.persistence.models.business import BusinessProfile
from app.persistence.models.file import UploadedFile
from app.persistence.models.ingestion_schema_decision import IngestionSchemaDecision
from app.persistence.models.repair import DataRepairItem, DataRepairRun
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import ExpenseEntry
from app.tests.integration import _reread_scenario as esc

TEST_PG_DSN = os.environ.get("TEST_PG_DSN")

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not TEST_PG_DSN, reason="requiere TEST_PG_DSN (Postgres real)"),
]

_DDL_ADVISORY_KEY = 0x5645_4B54_4F52_4D52  # "VEKTOR" + MR
_CTX = "sheet:catalogo"
_MAPEO = {"producto": "name", "cantidad": "stock_units", "costo": "unit_cost_ars"}


def _summary(eleccion: Any) -> dict[str, Any]:
    filas = [
        {"producto": "Vela aromática", "cantidad": "10", "costo": "1200", "__context__": _CTX},
        # Tres decimales (como una celda calculada del Excel): el gasto se calcula
        # con 1.234,567 × 10 = 12.345,67 y el movimiento (``Numeric(12,2)``)
        # guarda 1.234,57, que da 12.345,70. Sólo Postgres redondea.
        {
            "producto": "Sahumerio lavanda",
            "cantidad": "10",
            "costo": "1.234,567",
            "__context__": _CTX,
        },
    ]
    return {
        "file_type": "spreadsheet",
        "inferred_type": "stock",
        "multi_sheet": True,
        "has_producto": True,
        "stock_detectado": filas,
        "stock_treatment": eleccion,
        "mapping_contexts": [
            {
                "context_id": _CTX,
                "label": "catalogo",
                "source_kind": "sheet",
                "entity_type": "product",
                "headers": ["producto", "cantidad", "costo"],
                "fields": None,
                "preview_rows": filas[:1],
                "row_count": 2,
            }
        ],
    }


@pytest_asyncio.fixture(scope="module")
async def pg_engine() -> AsyncGenerator[AsyncEngine, None]:
    assert TEST_PG_DSN is not None
    engine = await esc.crear_engine(TEST_PG_DSN, _DDL_ADVISORY_KEY)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def sm(pg_engine: AsyncEngine) -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    yield async_sessionmaker(pg_engine, expire_on_commit=False)


async def _limpiar(sm: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID) -> None:
    async with sm() as s:
        for modelo in (DataRepairItem, DecisionAuditLog, IngestionSchemaDecision):
            await s.execute(delete(modelo).where(modelo.tenant_id == tenant_id))
        await s.commit()
    await esc.limpiar(sm, tenant_id)


async def test_reparar_y_revertir_en_postgres(
    sm: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(importer, "ingestion_schema_decisions_enabled_for", lambda _t: True)
    tenant_id = uuid.uuid4()
    file_id = uuid.uuid4()
    eleccion = {_CTX: "purchase"}
    try:
        async with sm() as s:
            s.add(Tenant(tenant_id=tenant_id, legal_name="T", display_name="T"))
            await s.flush()
            s.add(
                BusinessProfile(
                    profile_id=uuid.uuid4(),
                    tenant_id=tenant_id,
                    vertical_code=Vertical.DECORACION_HOGAR.value,
                    data_mode="M0",
                    data_confidence="LOW",
                    onboarding_completed=False,
                )
            )
            s.add(
                UploadedFile(
                    id=file_id,
                    tenant_id=tenant_id,
                    original_filename="catalogo.xlsx",
                    s3_key="k",
                    content_type="application/vnd.ms-excel",
                    size_bytes=1,
                    purpose="ingestion",
                    processing_status="done",
                    # Lo que el confirm guarda: sin filas ni ``mapping_contexts``.
                    parsed_summary_json={
                        k: v
                        for k, v in _summary(eleccion).items()
                        if k not in ("stock_detectado", "mapping_contexts")
                    },
                )
            )
            await s.flush()
            await importer.insert_confirmed_data(
                s,
                tenant_id,
                _summary(eleccion),
                {"productos": True},
                context_mappings={_CTX: _MAPEO},
                context_confirmed={_CTX: True},
                stock_treatment=eleccion,
                uploaded_file_id=file_id,
            )
            await s.commit()

        async with sm() as s:
            r = await mr.reparar(s, tenant_id, aplicar=True)
            await s.commit()
        assert not r.excluidos
        assert r.anulados == 2 and r.decisiones_compra == 1 and r.run_id is not None

        async with sm() as s:
            vivos = (
                await s.execute(
                    select(ExpenseEntry).where(
                        ExpenseEntry.tenant_id == tenant_id, ExpenseEntry.voided_at.is_(None)
                    )
                )
            ).scalars().all()
            assert vivos == []
            archivo = await s.get(UploadedFile, file_id)
            assert archivo is not None
            assert archivo.parsed_summary_json["stock_treatment"] == {_CTX: "opening_balance"}
            decision = (
                await s.execute(
                    select(IngestionSchemaDecision).where(
                        IngestionSchemaDecision.tenant_id == tenant_id,
                        IngestionSchemaDecision.decision_type == "stock_treatment",
                    )
                )
            ).scalar_one()
            assert decision.payload == {"treatment": "opening_balance"}

        async with sm() as s:
            res = await mr.revertir(s, r.run_id)
            await s.commit()
        assert (res.revividos, res.salteados, res.elecciones, res.decisiones) == (2, 0, 1, 1)

        async with sm() as s:
            run = await s.get(DataRepairRun, r.run_id)
            assert run is not None and run.status == "REVERTED"
            archivo = await s.get(UploadedFile, file_id)
            assert archivo is not None
            assert archivo.parsed_summary_json["stock_treatment"] == eleccion
    finally:
        await _limpiar(sm, tenant_id)
