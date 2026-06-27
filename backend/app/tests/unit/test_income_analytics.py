"""Tests de los handlers analíticos de AgentIncome (Sprint 17, Stage 4)."""

import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.agents.income.agent import AgentIncome
from app.application.agents.shared.schemas import ActionType, AgentRequest, AgentTask
from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import SaleEntry


def _task(intent: str) -> AgentTask:
    return AgentTask(
        agent="agent_income",
        action_type=ActionType.ANALYZE_SALES_DATA,
        entities={"_intent": intent},
    )


def _req(tenant_id: uuid.UUID, msg: str = "") -> AgentRequest:
    return AgentRequest(user_id=str(uuid.uuid4()), business_id=str(tenant_id), message=msg)


@pytest_asyncio.fixture
async def seeded(db_session: AsyncSession) -> uuid.UUID:
    tid = uuid.uuid4()
    db_session.add(
        Tenant(
            tenant_id=tid,
            legal_name="T",
            display_name="T",
            currency="ARS",
            pricing_reference_mode="MEP",
            status="ACTIVE",
        )
    )
    star = Product(
        tenant_id=tid,
        name="Estrella",
        sale_price_ars=Decimal("1000"),
        unit_cost_ars=Decimal("500"),
        stock_units=20,
        is_active=True,
    )
    prob = Product(
        tenant_id=tid,
        name="Problema",
        sale_price_ars=Decimal("1000"),
        unit_cost_ars=Decimal("950"),
        stock_units=20,
        is_active=True,
    )
    db_session.add_all([star, prob])
    await db_session.commit()
    for i in range(8):
        db_session.add(
            SaleEntry(
                tenant_id=tid,
                product_id=star.id,
                amount=Decimal("1000"),
                quantity=1,
                transaction_date=date.today() - timedelta(days=i),
                payment_method="cash",
            )
        )
        db_session.add(
            SaleEntry(
                tenant_id=tid,
                product_id=prob.id,
                amount=Decimal("1000"),
                quantity=1,
                transaction_date=date.today() - timedelta(days=i),
                payment_method="cash",
            )
        )
    await db_session.commit()
    return tid


@pytest.mark.asyncio
async def test_ticket_promedio(db_session, seeded):
    agent = AgentIncome(db=db_session)
    resp = await agent.process(
        _req(seeded, "ticket promedio"), task=_task("analizar_ticket_promedio")
    )
    assert resp.status == "success"
    assert resp.result["summary"] == "analizar_ticket_promedio"


@pytest.mark.asyncio
async def test_rentabilidad(db_session, seeded):
    agent = AgentIncome(db=db_session)
    resp = await agent.process(_req(seeded), task=_task("analizar_rentabilidad_ventas"))
    assert resp.status == "success"
    assert resp.result["structured_data"]["by_product"]


@pytest.mark.asyncio
async def test_productos_estrella(db_session, seeded):
    agent = AgentIncome(db=db_session)
    resp = await agent.process(_req(seeded), task=_task("detectar_productos_estrella"))
    assert resp.status == "success"


@pytest.mark.asyncio
async def test_descuentos_pide_campo(db_session, seeded):
    agent = AgentIncome(db=db_session)
    resp = await agent.process(_req(seeded), task=_task("analizar_descuentos"))
    assert resp.result["summary"] == "descuentos_sin_datos"


@pytest.mark.asyncio
async def test_analyze_file_sin_adjunto(db_session, seeded):
    agent = AgentIncome(db=db_session)
    task = AgentTask(
        agent="agent_income",
        action_type=ActionType.ANALYZE_FILE,
        entities={"_intent": "analizar_archivo_cargado"},
    )
    resp = await agent.process(_req(seeded, "analizá esto"), task=task)
    assert resp.status == "requires_clarification"


@pytest.mark.asyncio
async def test_importar_archivo_productos_genera_import(db_session, seeded):
    """importar_archivo_productos → AgentIncome → IMPORT_TABULAR_FILE con productos=true."""
    from app.persistence.models.file import PROCESSING_STATUS_DONE, UploadedFile

    f = UploadedFile(
        tenant_id=seeded,
        uploaded_by=uuid.uuid4(),
        original_filename="catalogo.xlsx",
        s3_key="k",
        content_type="application/vnd.ms-excel",
        size_bytes=100,
        purpose="stock",
        status="uploaded",
        processing_status=PROCESSING_STATUS_DONE,
        parsed_summary_json={
            "inferred_type": "stock",
            "confidence": "HIGH",
            "rows_processed": 12,
            "stock_detectado": [{"nombre": "Coca", "precio": 1000}],
            "ventas_detectadas": [],
            "gastos_detectados": [],
        },
    )
    db_session.add(f)
    await db_session.commit()

    agent = AgentIncome(db=db_session)
    task = AgentTask(agent="agent_income", action_type=ActionType.IMPORT_TABULAR_FILE, entities={})
    req = AgentRequest(
        user_id=str(uuid.uuid4()),
        business_id=str(seeded),
        message="importá este catálogo",
        attachments=[{"file_id": str(f.id), "filename": "catalogo.xlsx"}],
    )
    resp = await agent.process(req, task=task)
    assert resp.result["action_type"] == ActionType.IMPORT_TABULAR_FILE
    assert resp.result["structured_data"]["confirmed_fields"]["productos"] is True


@pytest.mark.asyncio
async def test_normalizar_archivo_con_columnas_riesgosas(db_session, seeded):
    from app.persistence.models.file import PROCESSING_STATUS_DONE, UploadedFile

    uid = uuid.uuid4()
    f = UploadedFile(
        tenant_id=seeded,
        uploaded_by=uid,
        original_filename="sucio.xlsx",
        s3_key="k",
        content_type="application/vnd.ms-excel",
        size_bytes=100,
        purpose="ventas",
        status="uploaded",
        processing_status=PROCESSING_STATUS_DONE,
        parsed_summary_json={
            "inferred_type": "ventas",
            "confidence": "HIGH",
            "ventas_detectadas": [{"monto": 100}],
            "columns_at_risk": [
                {"column": "proveedor", "null_pct": 0.62, "recommendation": "drop"}
            ],
        },
    )
    db_session.add(f)
    await db_session.commit()

    agent = AgentIncome(db=db_session)
    task = AgentTask(
        agent="agent_income",
        action_type=ActionType.ANALYZE_FILE,
        entities={"_intent": "limpiar_normalizar_archivo"},
    )
    req = AgentRequest(
        user_id=str(uuid.uuid4()),
        business_id=str(seeded),
        message="limpiá esto",
        attachments=[{"file_id": str(f.id), "filename": "sucio.xlsx"}],
    )
    resp = await agent.process(req, task=task)
    assert resp.status == "success"
    assert resp.result["summary"] == "limpiar_normalizar_archivo"
    assert resp.result["structured_data"]["columns_at_risk"]
    assert "proveedor" in resp.message
