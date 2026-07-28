"""Tests del rescate de intent ambiguo en ChatOrchestrator (Sprint 17, Stages 0 + 0.5).

Cubre las dos capas determinísticas (DataIntentExtractor + IntentRescue) y los
cortes sin sub-agente (_NO_AGENT_INTENTS).
"""

import json
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
from app.tests.conftest import add_business_profile

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
    # Todo tenant real nace con perfil; sin él el orchestrator ya no asume
    # kiosco, pide configurar el negocio.
    await add_business_profile(db_session, tid)
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


@pytest_asyncio.fixture
async def tenant_with_imported_file(db_session: AsyncSession) -> tuple[uuid.UUID, str]:
    """Archivo ya importado: su summary tiene `imported_counts` (lo escribe
    pending_action_service al confirmar IMPORT_TABULAR_FILE). No debe re-ofrecerse."""
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
    # Todo tenant real nace con perfil; sin él el orchestrator ya no asume
    # kiosco, pide configurar el negocio.
    await add_business_profile(db_session, tid)
    f = UploadedFile(
        tenant_id=tid,
        uploaded_by=uid,
        original_filename="ventas.xlsx",
        s3_key="k",
        content_type="application/vnd.ms-excel",
        size_bytes=100,
        purpose="chat",
        status="uploaded",
        processing_status=PROCESSING_STATUS_DONE,
        parsed_summary_json={
            "inferred_type": "ventas",
            "confidence": "HIGH",
            "ventas_detectadas": [{"monto": 1000}],
            "gastos_detectados": [],
            "stock_detectado": [],
            # marcador de que YA se importó
            "imported_counts": {"ventas": 1, "gastos": 0, "productos": 0},
            "confirmed_fields": {"ventas": True, "gastos": False, "productos": False},
        },
    )
    db_session.add(f)
    await db_session.commit()
    return tid, str(f.id)


def test_can_reuse_pending_file_excludes_manual_register():
    """#1(b): un registro manual sin término de archivo NO reactiva el pending file.
    Un pedido sobre el archivo (con término de archivo) sí lo hace."""
    reuse = ChatOrchestrator._can_reuse_pending_file
    # registro manual de una operación puntual → NO reusa
    assert reuse("anotame una venta de $1200") is False
    assert reuse("registrá un gasto de $500 de luz") is False
    # pedidos que sí refieren al archivo → reusa
    assert reuse("importá y anotá los registros donde corresponde") is True
    assert reuse("anotá los datos del archivo") is True
    assert reuse("cargá esto") is True


@pytest.mark.asyncio
async def test_recall_skips_already_imported_file(db_session, tenant_with_imported_file):
    """#1(a): _recall_pending_files filtra archivos que ya tienen imported_counts."""
    tid, file_id = tenant_with_imported_file
    conv_id = str(uuid.uuid4())
    pending_payload = json.dumps(
        {
            "file_ids": [file_id],
            "attachments": [{"file_id": file_id, "filename": "ventas.xlsx"}],
            "ts": 1,
        }
    )
    redis = AsyncMock()
    redis.get = AsyncMock(return_value=pending_payload)

    orch = ChatOrchestrator.__new__(ChatOrchestrator)
    recalled = await orch._recall_pending_files(conv_id, redis, db_session, tid)
    assert recalled == []


@pytest.mark.asyncio
async def test_handle_does_not_reimport_already_imported_file(
    db_session, tenant_with_imported_file
):
    """E2E del #1: tras importar, un turno de registro manual NO re-inyecta el archivo
    ya importado (evita duplicar datos financieros)."""
    tid, file_id = tenant_with_imported_file
    conv_id = str(uuid.uuid4())
    pending_payload = json.dumps(
        {
            "file_ids": [file_id],
            "attachments": [{"file_id": file_id, "filename": "ventas.xlsx"}],
            "ts": 1,
        }
    )

    async def redis_get(key: str):
        if key == ChatOrchestrator._pending_file_key(conv_id):
            return pending_payload
        return None

    redis = AsyncMock()
    redis.get = AsyncMock(side_effect=redis_get)
    redis.setex = AsyncMock()

    exec_resp = AgentResponse(
        request_id="r1",
        agent_name="agent_income",
        status="success",
        risk_level=RiskLevel.LOW,
        confidence=Confidence.HIGH,
        message="Anoté la venta.",
        result={"summary": "venta"},
    )

    captured_request: AgentRequest | None = None

    async def execute(plan, request):
        nonlocal captured_request
        captured_request = request
        return [exec_resp]

    orch = ChatOrchestrator.__new__(ChatOrchestrator)
    orch.client = AsyncMock()
    # registro manual con monto puntual, en la misma conversación que el import previo
    req = AgentRequest(
        user_id=str(uuid.uuid4()),
        business_id=str(tid),
        message="anotame una venta de $1200",
        conversation_id=conv_id,
        attachments=[],
    )

    ceo_resp = AgentResponse(
        request_id="r1",
        agent_name="agent_ceo",
        status="success",
        risk_level=RiskLevel.LOW,
        confidence=Confidence.HIGH,
        result={
            "intent": "ingresar_venta",
            "target_agent": "agent_income",
            "plan": {
                "plan_id": "p",
                "intent": "ingresar_venta",
                "tasks": [
                    {
                        "task_id": "t",
                        "agent": "agent_income",
                        "action_type": "REGISTER_SALE",
                        "entities": {"amount": 1200},
                        "depends_on": [],
                        "approval_group": None,
                    }
                ],
                "requires_synthesis": False,
                "fallback_message": None,
            },
        },
    )

    with (
        patch(f"{ORCHESTRATOR}.AgentCEO") as mock_ceo_cls,
        patch(f"{ORCHESTRATOR}.TeamPlanExecutor") as mock_exec_cls,
    ):
        mock_ceo_cls.return_value.process = AsyncMock(return_value=ceo_resp)
        mock_exec_cls.return_value.execute = AsyncMock(side_effect=execute)
        await orch.handle(req, db_session, redis, uuid.UUID(req.user_id), tid)

    # El archivo ya importado NO se reinyecta en el turno de registro manual.
    assert captured_request is not None
    assert captured_request.attachments == []


@pytest.mark.asyncio
async def test_handle_reuses_pending_file_for_import_turn(db_session, tenant_with_product_file):
    tid, file_id = tenant_with_product_file
    conv_id = str(uuid.uuid4())
    pending_payload = json.dumps(
        {
            "file_ids": [file_id],
            "attachments": [{"file_id": file_id, "filename": "lista.xlsx"}],
            "ts": 1,
        }
    )

    async def redis_get(key: str):
        if key == ChatOrchestrator._pending_file_key(conv_id):
            return pending_payload
        return None

    redis = AsyncMock()
    redis.get = AsyncMock(side_effect=redis_get)
    redis.setex = AsyncMock()

    exec_resp = AgentResponse(
        request_id="r1",
        agent_name="agent_income",
        status="success",
        risk_level=RiskLevel.LOW,
        confidence=Confidence.HIGH,
        message="Preparé la importación del catálogo.",
        result={"summary": "importar productos"},
    )

    captured_request: AgentRequest | None = None

    async def execute(plan, request):
        nonlocal captured_request
        captured_request = request
        return [exec_resp]

    orch = ChatOrchestrator.__new__(ChatOrchestrator)
    orch.client = AsyncMock()
    req = AgentRequest(
        user_id=str(uuid.uuid4()),
        business_id=str(tid),
        message="importa y anota los registros donde corresponde",
        conversation_id=conv_id,
        attachments=[],
    )

    with (
        patch(f"{ORCHESTRATOR}.AgentCEO") as mock_ceo_cls,
        patch(f"{ORCHESTRATOR}.TeamPlanExecutor") as mock_exec_cls,
    ):
        mock_ceo_cls.return_value.process = AsyncMock(return_value=_ceo_unknown_response())
        mock_exec_cls.return_value.execute = AsyncMock(side_effect=execute)
        resp = await orch.handle(req, db_session, redis, uuid.UUID(req.user_id), tid)

    assert resp.result.get("intent") == "importar_archivo_productos"
    assert captured_request is not None
    assert captured_request.attachments == [{"file_id": file_id, "filename": "lista.xlsx"}]


@pytest.mark.asyncio
async def test_handle_does_not_reuse_pending_file_when_topic_changes(
    db_session,
    tenant_with_product_file,
):
    tid, file_id = tenant_with_product_file
    conv_id = str(uuid.uuid4())
    pending_payload = json.dumps(
        {
            "file_ids": [file_id],
            "attachments": [{"file_id": file_id, "filename": "lista.xlsx"}],
            "ts": 1,
        }
    )

    async def redis_get(key: str):
        if key == ChatOrchestrator._pending_file_key(conv_id):
            return pending_payload
        return None

    redis = AsyncMock()
    redis.get = AsyncMock(side_effect=redis_get)
    redis.setex = AsyncMock()

    ceo_resp = AgentResponse(
        request_id="r1",
        agent_name="agent_ceo",
        status="success",
        risk_level=RiskLevel.LOW,
        confidence=Confidence.HIGH,
        result={
            "intent": "analizar_ventas",
            "target_agent": "agent_income",
            "plan": {
                "plan_id": "p",
                "intent": "analizar_ventas",
                "tasks": [
                    {
                        "task_id": "t",
                        "agent": "agent_income",
                        "action_type": "ANALYZE_SALES_DATA",
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
    exec_resp = AgentResponse(
        request_id="r1",
        agent_name="agent_income",
        status="success",
        risk_level=RiskLevel.LOW,
        confidence=Confidence.HIGH,
        message="Ayer vendiste...",
        result={"summary": "ventas"},
    )

    captured_request: AgentRequest | None = None

    async def execute(plan, request):
        nonlocal captured_request
        captured_request = request
        return [exec_resp]

    orch = ChatOrchestrator.__new__(ChatOrchestrator)
    orch.client = AsyncMock()
    req = AgentRequest(
        user_id=str(uuid.uuid4()),
        business_id=str(tid),
        message="cuánto vendí ayer",
        conversation_id=conv_id,
        attachments=[],
    )

    with (
        patch(f"{ORCHESTRATOR}.AgentCEO") as mock_ceo_cls,
        patch(f"{ORCHESTRATOR}.TeamPlanExecutor") as mock_exec_cls,
    ):
        mock_ceo_cls.return_value.process = AsyncMock(return_value=ceo_resp)
        mock_exec_cls.return_value.execute = AsyncMock(side_effect=execute)
        await orch.handle(req, db_session, redis, uuid.UUID(req.user_id), tid)

    assert captured_request is not None
    assert captured_request.attachments == []


@pytest.mark.asyncio
async def test_handle_unknown_off_topic_does_not_offer_pending_file(
    db_session,
    tenant_with_product_file,
):
    tid, file_id = tenant_with_product_file
    conv_id = str(uuid.uuid4())
    pending_payload = json.dumps(
        {
            "file_ids": [file_id],
            "attachments": [{"file_id": file_id, "filename": "lista.xlsx"}],
            "ts": 1,
        }
    )

    async def redis_get(key: str):
        if key == ChatOrchestrator._pending_file_key(conv_id):
            return pending_payload
        return None

    redis = AsyncMock()
    redis.get = AsyncMock(side_effect=redis_get)
    redis.setex = AsyncMock()

    orch = ChatOrchestrator.__new__(ChatOrchestrator)
    orch.client = AsyncMock()
    req = AgentRequest(
        user_id=str(uuid.uuid4()),
        business_id=str(tid),
        message="contame un chiste",
        conversation_id=conv_id,
        attachments=[],
    )

    with (
        patch(f"{ORCHESTRATOR}.AgentCEO") as mock_ceo_cls,
        patch(f"{ORCHESTRATOR}.TeamPlanExecutor") as mock_exec_cls,
    ):
        mock_ceo_cls.return_value.process = AsyncMock(return_value=_ceo_unknown_response())
        resp = await orch.handle(req, db_session, redis, uuid.UUID(req.user_id), tid)

    assert resp.result.get("intent") == "out_of_scope"
    assert req.attachments == []
    mock_exec_cls.return_value.execute.assert_not_called()
