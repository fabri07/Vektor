from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.agent import _attach_conversation_id
from app.application.agents.cash.agent import AgentCash
from app.application.agents.shared.schemas import (
    ActionType,
    AgentRequest,
    AgentResponse,
    Confidence,
    RiskLevel,
)
from app.application.agents.chat.agent import AgentChat
from app.application.services.chat_orchestrator import ChatOrchestrator
from app.application.services.pending_action_service import execute_pending_action
from app.persistence.models.chat_session_log import ChatSessionLog
from app.persistence.models.file import PROCESSING_STATUS_DONE, UploadedFile
from app.persistence.models.pending_action import PendingAction
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import SaleEntry
from app.persistence.models.user import User


@pytest.fixture
def redis_mock() -> AsyncMock:
    redis = AsyncMock()
    redis.get = AsyncMock(return_value=None)
    redis.setex = AsyncMock()
    redis.delete = AsyncMock()
    return redis


def _sales_summary() -> dict[str, Any]:
    return {
        "file_type": "spreadsheet",
        "inferred_type": "ventas",
        "confidence": "HIGH",
        "has_venta": True,
        "ventas_detectadas": [
            {"fecha": "2026-05-03", "monto": "1200", "descripcion": "Empanadas"},
        ],
        "gastos_detectados": [],
        "stock_detectado": [],
        "rows_processed": 1,
    }


@pytest.mark.asyncio
async def test_import_tabular_file_inserta_en_sale_entry(
    db_session: AsyncSession,
    sample_tenant: Tenant,
    sample_user: User,
    redis_mock: AsyncMock,
) -> None:
    uploaded = UploadedFile(
        tenant_id=sample_tenant.tenant_id,
        uploaded_by=sample_user.user_id,
        original_filename="ventas.csv",
        s3_key="uploads/test/ventas.csv",
        content_type="text/csv",
        size_bytes=100,
        purpose="chat",
        status="uploaded",
        processing_status=PROCESSING_STATUS_DONE,
        parsed_summary_json=_sales_summary(),
    )
    db_session.add(uploaded)
    await db_session.flush()
    action = PendingAction(
        tenant_id=sample_tenant.tenant_id,
        user_id=sample_user.user_id,
        action_type=ActionType.IMPORT_TABULAR_FILE,
        payload={
            "file_id": str(uploaded.id),
            "conversation_id": "conv-import",
            "confirmed_fields": {"ventas": True},
        },
        risk_level="MEDIUM",
    )
    db_session.add(action)
    await db_session.flush()

    await execute_pending_action(action, db_session, redis=redis_mock)

    sale = (await db_session.execute(select(SaleEntry))).scalar_one()
    assert sale.amount == Decimal("1200")
    assert sale.provenance == "REAL"
    log = (await db_session.execute(select(ChatSessionLog))).scalar_one()
    assert log.entry_type == "DATA_LOADED"
    assert log.uploaded_file_id == uploaded.id


@pytest.mark.asyncio
async def test_import_tabular_file_sin_file_id_lanza_error(
    db_session: AsyncSession,
    sample_tenant: Tenant,
    sample_user: User,
) -> None:
    action = PendingAction(
        tenant_id=sample_tenant.tenant_id,
        user_id=sample_user.user_id,
        action_type=ActionType.IMPORT_TABULAR_FILE,
        payload={"conversation_id": "conv-import"},
        risk_level="MEDIUM",
    )
    with pytest.raises(ValueError, match="sin file_id"):
        await execute_pending_action(action, db_session)


@pytest.mark.asyncio
async def test_register_sale_registra_data_loaded(
    db_session: AsyncSession,
    sample_tenant: Tenant,
    sample_user: User,
    redis_mock: AsyncMock,
) -> None:
    action = PendingAction(
        tenant_id=sample_tenant.tenant_id,
        user_id=sample_user.user_id,
        action_type=ActionType.REGISTER_SALE,
        payload={"amount": "400", "payment_method": "cash", "conversation_id": "conv-sale"},
        risk_level="MEDIUM",
    )
    db_session.add(action)
    await db_session.flush()
    with patch("app.application.agents.income.agent.AgentIncome.on_confirmed_sale", AsyncMock()):
        await execute_pending_action(action, db_session, redis=redis_mock)

    log = (await db_session.execute(select(ChatSessionLog))).scalar_one()
    assert log.session_id == "conv-sale"
    assert log.entity_type == "sale"


@pytest.mark.asyncio
async def test_contexto_proxima_sesion_incluye_ultima_carga(
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    from app.application.services.chat_memory_service import ChatMemoryService

    svc = ChatMemoryService()
    await svc.record(db_session, sample_tenant.tenant_id, "conv-a", "DATA_LOADED", "Ventas mayo")
    ctx = await svc.get_session_context(db_session, sample_tenant.tenant_id)
    rendered = AgentChat._render_session_memory(ctx)
    assert "Última carga: Ventas mayo" in rendered
    assert "No repitas preguntas" in rendered


def test_orchestrator_inyecta_historial_en_memoria_llm() -> None:
    rendered = AgentChat._render_session_memory(
        {
            "historial_disponible": True,
            "ultima_carga": {
                "descripcion": "Importado ventas.csv",
                "registros": 5,
                "fecha": "2026-05-03T10:00:00",
            },
            "total_cargas_registradas": 2,
        }
    )
    assert "HISTORIAL DE CARGAS" in rendered
    assert "Importado ventas.csv" in rendered


def test_conversation_id_se_propaga_a_structured_data() -> None:
    response = AgentResponse(
        request_id="r1",
        agent_name="agent_cash",
        status="requires_approval",
        risk_level=RiskLevel.MEDIUM,
        confidence=Confidence.HIGH,
        requires_approval=True,
        result={
            "action_type": ActionType.REGISTER_SALE,
            "structured_data": {"amount": "100"},
        },
    )
    _attach_conversation_id(response, "conv-123")
    assert response.result["structured_data"]["conversation_id"] == "conv-123"


def test_conversation_id_no_pisa_payload_existente() -> None:
    response = AgentResponse(
        request_id="r1",
        agent_name="agent_supplier",
        status="requires_approval",
        risk_level=RiskLevel.MEDIUM,
        confidence=Confidence.HIGH,
        requires_approval=True,
        result={
            "action_type": ActionType.CREATE_SUPPLIER_DRAFT,
            "payload": {"to": "proveedor@example.com", "body": "Hola"},
        },
    )
    _attach_conversation_id(response, "conv-456")
    assert response.result["payload"]["to"] == "proveedor@example.com"
    assert response.result["payload"]["conversation_id"] == "conv-456"
    assert "structured_data" not in response.result


@pytest.mark.asyncio
async def test_import_tabular_file_google_sheets_parsed_records_inserta_sale(
    db_session: AsyncSession,
    sample_tenant: Tenant,
    sample_user: User,
    redis_mock: AsyncMock,
) -> None:
    action = PendingAction(
        tenant_id=sample_tenant.tenant_id,
        user_id=sample_user.user_id,
        action_type=ActionType.IMPORT_TABULAR_FILE,
        payload={
            "source": "google_sheets",
            "record_type": "sales",
            "conversation_id": "conv-sheets",
            "parsed_records": [
                {"fecha": "2026-05-03", "monto": "1200", "producto": "Yerba"},
                {"fecha": "2026-05-03", "monto": "2500", "producto": "Azucar"},
            ],
        },
        risk_level="MEDIUM",
    )
    db_session.add(action)
    await db_session.flush()

    await execute_pending_action(action, db_session, redis=redis_mock)

    sales = list((await db_session.execute(select(SaleEntry))).scalars().all())
    assert [sale.amount for sale in sales] == [Decimal("1200"), Decimal("2500")]
    log = (await db_session.execute(select(ChatSessionLog))).scalar_one()
    assert log.session_id == "conv-sheets"
    assert log.entity_count == 2
    assert log.uploaded_file_id is None


@pytest.mark.asyncio
async def test_agent_cash_adjunto_genera_import_tabular_file(
    db_session: AsyncSession,
    sample_tenant: Tenant,
    sample_user: User,
) -> None:
    uploaded = UploadedFile(
        tenant_id=sample_tenant.tenant_id,
        uploaded_by=sample_user.user_id,
        original_filename="ventas.csv",
        s3_key="uploads/test/ventas.csv",
        content_type="text/csv",
        size_bytes=100,
        purpose="chat",
        status="uploaded",
        processing_status=PROCESSING_STATUS_DONE,
        parsed_summary_json=_sales_summary(),
    )
    db_session.add(uploaded)
    await db_session.flush()

    response = await AgentCash(db=db_session).process(
        AgentRequest(
            user_id=str(sample_user.user_id),
            business_id=str(sample_tenant.tenant_id),
            message="cargá este archivo",
            attachments=[{"file_id": str(uploaded.id), "filename": "ventas.csv"}],
            conversation_id="conv-import",
        )
    )

    assert response.requires_approval is True
    assert response.result["action_type"] == ActionType.IMPORT_TABULAR_FILE
    assert response.result["structured_data"]["file_id"] == str(uploaded.id)
