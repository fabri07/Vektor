"""Tests del rescate de intent ambiguo en ChatOrchestrator (Sprint 17, Stages 0 + 0.5).

Cubre las dos capas determinísticas (DataIntentExtractor + IntentRescue) y los
cortes sin sub-agente (_NO_AGENT_INTENTS).
"""

import uuid
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.agents.shared.schemas import (
    AgentRequest,
    AgentResponse,
    Confidence,
    RiskLevel,
)
from app.application.services.chat_orchestrator import (
    _NO_AGENT_INTENTS,
    _NO_AGENT_MESSAGES,
    _OUT_OF_SCOPE_MESSAGE,
    ChatOrchestrator,
)
from app.persistence.models.file import PROCESSING_STATUS_DONE, UploadedFile
from app.persistence.models.tenant import Tenant

ORCHESTRATOR = "app.application.services.chat_orchestrator"


def test_no_agent_constants_aligned():
    # Cada intent de corte tiene un mensaje fijo asociado
    assert set(_NO_AGENT_MESSAGES) == _NO_AGENT_INTENTS
    assert _NO_AGENT_MESSAGES["pedir_aclaracion_negocio"] != _NO_AGENT_MESSAGES["out_of_scope"]
    assert (
        _NO_AGENT_MESSAGES["pedir_aclaracion_sobre_archivo"] != _NO_AGENT_MESSAGES["out_of_scope"]
    )


@pytest_asyncio.fixture
async def tenant_with_product_file(db_session: AsyncSession) -> tuple[uuid.UUID, str]:
    tid = uuid.uuid4()
    uid = uuid.uuid4()
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
    f = UploadedFile(
        tenant_id=tid,
        uploaded_by=uid,
        original_filename="lista.xlsx",
        s3_key="k",
        content_type="application/vnd.ms-excel",
        size_bytes=100,
        purpose="stock",
        status="uploaded",
        processing_status=PROCESSING_STATUS_DONE,
        parsed_summary_json={
            "inferred_type": "stock",
            "confidence": "HIGH",
            "stock_detectado": [{"nombre": "Coca", "precio": 1000}],
            "ventas_detectadas": [],
            "gastos_detectados": [],
        },
    )
    db_session.add(f)
    await db_session.commit()
    return tid, str(f.id)


@pytest.mark.asyncio
async def test_rescue_product_attachment_maps_to_lista_precios(
    db_session, tenant_with_product_file
):
    tid, file_id = tenant_with_product_file
    orch = ChatOrchestrator.__new__(ChatOrchestrator)  # sin __init__ (evita cliente Anthropic)
    req = AgentRequest(
        user_id=str(uuid.uuid4()),
        business_id=str(tid),
        message="analizá esto",
        attachments=[{"file_id": file_id, "filename": "lista.xlsx"}],
    )
    intent, entities = await orch._rescue_unknown_intent(req, tid, db_session)
    assert intent == "analizar_precios"
    assert entities == {"analysis_type": "lista"}


@pytest.mark.asyncio
async def test_rescue_no_attachment_uses_text(db_session):
    tid = uuid.uuid4()
    orch = ChatOrchestrator.__new__(ChatOrchestrator)
    req = AgentRequest(
        user_id=str(uuid.uuid4()),
        business_id=str(tid),
        message="cuánto gano con cada producto",
        attachments=[],
    )
    intent, entities = await orch._rescue_unknown_intent(req, tid, db_session)
    assert intent == "analizar_precios"
    assert entities == {"analysis_type": "margenes"}


@pytest.mark.asyncio
async def test_rescue_off_topic_out_of_scope(db_session):
    tid = uuid.uuid4()
    orch = ChatOrchestrator.__new__(ChatOrchestrator)
    req = AgentRequest(
        user_id=str(uuid.uuid4()), business_id=str(tid), message="contame un chiste", attachments=[]
    )
    intent, _ = await orch._rescue_unknown_intent(req, tid, db_session)
    assert intent == "out_of_scope"
    assert intent in _NO_AGENT_INTENTS


def _ceo_unknown_response() -> AgentResponse:
    """Simula el CEO devolviendo intent_desconocido (el incidente original)."""
    return AgentResponse(
        request_id="r1",
        agent_name="agent_ceo",
        status="success",
        risk_level=RiskLevel.LOW,
        confidence=Confidence.HIGH,
        result={
            "intent": "intent_desconocido",
            "target_agent": "agent_helper",
            "plan": {
                "plan_id": "p",
                "intent": "intent_desconocido",
                "tasks": [
                    {
                        "task_id": "t",
                        "agent": "agent_helper",
                        "action_type": "ANSWER_HELP_REQUEST",
                        "entities": {},
                        "depends_on": [],
                        "approval_group": None,
                    }
                ],
                "requires_synthesis": False,
                "fallback_message": None,
            },
        },
    )


@pytest.mark.asyncio
async def test_handle_e2e_price_attachment_not_out_of_scope(db_session, tenant_with_product_file):
    """E2E del incidente: CEO falla con intent_desconocido + adjunto de precios →
    handle() debe rescatar a analizar_precios, NO cortar con out_of_scope."""
    tid, file_id = tenant_with_product_file

    # Respuesta del sub-agente (stock) tras el rescate — análisis determinístico
    exec_resp = AgentResponse(
        request_id="r1",
        agent_name="agent_stock",
        status="success",
        risk_level=RiskLevel.LOW,
        confidence=Confidence.HIGH,
        message="Leí una lista con 1 productos.",
        result={"summary": "analizar_lista_precios", "analysis": True},
    )

    redis = AsyncMock()
    redis.get = AsyncMock(return_value=None)
    redis.setex = AsyncMock()

    orch = ChatOrchestrator.__new__(ChatOrchestrator)
    orch.client = AsyncMock()

    req = AgentRequest(
        user_id=str(uuid.uuid4()),
        business_id=str(tid),
        message="analizá esto",
        conversation_id=None,
        attachments=[{"file_id": file_id, "filename": "lista.xlsx"}],
    )

    with (
        patch(f"{ORCHESTRATOR}.AgentCEO") as mock_ceo_cls,
        patch(f"{ORCHESTRATOR}.TeamPlanExecutor") as mock_exec_cls,
    ):
        mock_ceo_cls.return_value.process = AsyncMock(return_value=_ceo_unknown_response())
        mock_exec_cls.return_value.execute = AsyncMock(return_value=[exec_resp])
        resp = await orch.handle(req, db_session, redis, uuid.UUID(req.user_id), tid)

    # El rescate convirtió intent_desconocido → analizar_precios
    assert resp.result.get("intent") == "analizar_precios"
    assert resp.status != "error"
    # NO debe ser el corte de fuera de scope
    assert resp.message != _OUT_OF_SCOPE_MESSAGE
