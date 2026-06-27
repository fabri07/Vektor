"""Tests del solapamiento CEO + carga de contexto (Task 4 — F0.6).

Verifica:
- attachment_meta disponible en request.context cuando el CEO lo recibe (orden preservado).
- Flujo end-to-end single-task feliz da el mismo resultado observable.
- Flujo end-to-end con adjunto (attachment_meta populated).
- Si ceo.process lanza, el orchestrator devuelve el fallback esperado sin task huérfano ni crash.
"""

from __future__ import annotations

import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.application.agents.shared.schemas import (
    AgentRequest,
    AgentResponse,
    Confidence,
    LLMCall,
    RiskLevel,
    UsageSummary,
)
from app.application.services.chat_orchestrator import ChatOrchestrator

ORCHESTRATOR = "app.application.services.chat_orchestrator"

# ── Helpers ────────────────────────────────────────────────────────────────────


def _make_request(with_attachment: bool = False) -> AgentRequest:
    req = AgentRequest(
        user_id=str(uuid.uuid4()),
        business_id=str(uuid.uuid4()),
        message="registrá una venta de $1500",
    )
    if with_attachment:
        req.attachments = [{"file_id": str(uuid.uuid4()), "filename": "ventas.csv"}]
    return req


def _make_ceo_response(request_id: str) -> AgentResponse:
    plan_dict = {
        "plan_id": str(uuid.uuid4()),
        "intent": "ingresar_venta",
        "tasks": [
            {
                "task_id": str(uuid.uuid4()),
                "agent": "agent_income",
                "action_type": "REGISTER_SALE",
                "entities": {},
                "depends_on": [],
                "approval_group": None,
            }
        ],
        "requires_synthesis": False,
        "fallback_message": None,
    }
    return AgentResponse(
        request_id=request_id,
        agent_name="agent_ceo",
        status="success",
        risk_level=RiskLevel.LOW,
        requires_approval=False,
        confidence=Confidence.HIGH,
        result={
            "intent": "ingresar_venta",
            "action_type": "REGISTER_SALE",
            "target_agent": "agent_income",
            "plan": plan_dict,
        },
        usage=UsageSummary(
            calls=[LLMCall(source="ceo", model="claude-sonnet-4-6", input_tokens=50, output_tokens=20)]
        ),
    )


def _make_sub_response(request_id: str) -> AgentResponse:
    return AgentResponse(
        request_id=request_id,
        agent_name="agent_income",
        status="success",
        risk_level=RiskLevel.LOW,
        requires_approval=True,
        confidence=Confidence.HIGH,
        result={"summary": "Registrar venta de $1500", "action_type": "REGISTER_SALE"},
    )


@pytest.fixture
def mock_db() -> AsyncMock:
    db = AsyncMock()
    tenant = MagicMock()
    tenant.display_name = "Kiosco Test"
    db.get = AsyncMock(return_value=tenant)
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = MagicMock(vertical_code="kiosco_almacen")
    result_mock.scalars.return_value.all.return_value = []
    db.execute = AsyncMock(return_value=result_mock)
    return db


@pytest.fixture
def mock_redis() -> AsyncMock:
    redis = AsyncMock()
    redis.get = AsyncMock(return_value=None)
    redis.setex = AsyncMock()
    return redis


# ── Tests ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_attachment_meta_available_to_ceo(mock_db: AsyncMock, mock_redis: AsyncMock) -> None:
    """attachment_meta está en request.context cuando el CEO lo recibe.

    Verificamos que, aunque el CEO corra como asyncio.Task en paralelo con las
    cargas de contexto, el campo attachment_meta ya estaba en request.context
    cuando se lanzó el task (se calcula antes del create_task).
    """
    request = _make_request(with_attachment=True)

    captured_contexts: list[dict] = []

    async def fake_ceo_process(req: AgentRequest) -> AgentResponse:
        # Capturar el context en el momento en que el CEO lo lee
        captured_contexts.append(dict(req.context))
        return _make_ceo_response(req.request_id)

    with (
        patch(f"{ORCHESTRATOR}.AgentCEO") as mock_ceo_cls,
        patch(f"{ORCHESTRATOR}.TeamPlanExecutor") as mock_executor,
        patch(f"{ORCHESTRATOR}.AgentChat"),
        patch(f"{ORCHESTRATOR}.get_anthropic_async_client"),
        patch(f"{ORCHESTRATOR}.BusinessMemoryService") as mock_bm,
        patch(f"{ORCHESTRATOR}.AgentMemoryService") as mock_am,
        patch(f"{ORCHESTRATOR}.ConversationService") as mock_conv,
    ):
        mock_ceo_cls.return_value.process = fake_ceo_process
        mock_executor.return_value.execute = AsyncMock(
            return_value=[_make_sub_response(request.request_id)]
        )
        mock_bm.return_value.get = AsyncMock(return_value={})
        mock_am.return_value.get_context_fragment = AsyncMock(return_value="")
        conv_svc = AsyncMock()
        conv_svc.get_context = AsyncMock(return_value={"turns": [], "summary": None})
        mock_conv.return_value = conv_svc

        orchestrator = ChatOrchestrator()
        response = await orchestrator.handle(
            request, mock_db, mock_redis, uuid.uuid4(), uuid.uuid4()
        )

    assert response.status in ("success", "requires_approval", "requires_clarification")
    assert len(captured_contexts) == 1, "El CEO debe haber sido invocado exactamente una vez"
    ctx = captured_contexts[0]
    assert "attachment_meta" in ctx, (
        "attachment_meta debe estar en request.context cuando el CEO lo recibe"
    )
    assert ctx["attachment_meta"]["has_attachment"] is True, (
        "has_attachment debe ser True cuando hay adjuntos"
    )


@pytest.mark.asyncio
async def test_end_to_end_single_task_success(mock_db: AsyncMock, mock_redis: AsyncMock) -> None:
    """Flujo feliz single-task: resultado idéntico al pre-parallelización."""
    request = _make_request()
    _mock_chat_call = LLMCall(
        source="agent_chat", model="claude-sonnet-4-6", input_tokens=100, output_tokens=50
    )

    with (
        patch(f"{ORCHESTRATOR}.AgentCEO") as mock_ceo_cls,
        patch(f"{ORCHESTRATOR}.TeamPlanExecutor") as mock_executor,
        patch(f"{ORCHESTRATOR}.AgentChat") as mock_chat_cls,
        patch(f"{ORCHESTRATOR}.get_anthropic_async_client"),
        patch(f"{ORCHESTRATOR}.BusinessMemoryService") as mock_bm,
        patch(f"{ORCHESTRATOR}.AgentMemoryService") as mock_am,
        patch(f"{ORCHESTRATOR}.ConversationService") as mock_conv,
    ):
        mock_ceo_cls.return_value.process = AsyncMock(
            side_effect=lambda req: _make_ceo_response(req.request_id)
        )
        sub_resp = _make_sub_response(request.request_id)
        sub_resp.requires_approval = False
        mock_executor.return_value.execute = AsyncMock(return_value=[sub_resp])
        mock_chat_cls.return_value.generate_response = AsyncMock(
            return_value=("Venta registrada correctamente.", _mock_chat_call)
        )
        mock_bm.return_value.get = AsyncMock(return_value={})
        mock_am.return_value.get_context_fragment = AsyncMock(return_value="")
        conv_svc = AsyncMock()
        conv_svc.get_context = AsyncMock(return_value={"turns": [], "summary": None})
        mock_conv.return_value = conv_svc

        orchestrator = ChatOrchestrator()
        response = await orchestrator.handle(
            request, mock_db, mock_redis, uuid.uuid4(), uuid.uuid4()
        )

    assert response.status == "success"
    assert response.message == "Venta registrada correctamente."
    # CEO LLM call debe estar en usage
    assert response.usage is not None
    assert any(c.source == "ceo" for c in response.usage.calls), (
        "La LLMCall del CEO debe estar en el usage acumulado"
    )
    # intent y target_agent deben estar en result
    assert response.result.get("intent") == "ingresar_venta"
    assert response.result.get("target_agent") == "agent_income"


@pytest.mark.asyncio
async def test_ceo_exception_returns_fallback_no_crash(
    mock_db: AsyncMock, mock_redis: AsyncMock
) -> None:
    """Si ceo.process lanza RuntimeError, el orchestrator devuelve el fallback de error.

    El task NO debe quedar huérfano: el handle() lo awaitea en el except y retorna.
    """
    request = _make_request()

    with (
        patch(f"{ORCHESTRATOR}.AgentCEO") as mock_ceo_cls,
        patch(f"{ORCHESTRATOR}.TeamPlanExecutor") as mock_executor,
        patch(f"{ORCHESTRATOR}.AgentChat"),
        patch(f"{ORCHESTRATOR}.get_anthropic_async_client"),
        patch(f"{ORCHESTRATOR}.BusinessMemoryService") as mock_bm,
        patch(f"{ORCHESTRATOR}.AgentMemoryService") as mock_am,
        patch(f"{ORCHESTRATOR}.ConversationService") as mock_conv,
    ):
        mock_ceo_cls.return_value.process = AsyncMock(
            side_effect=RuntimeError("Fallo simulado del CEO")
        )
        mock_executor_instance = mock_executor.return_value
        mock_executor_instance.execute = AsyncMock(return_value=[])
        mock_bm.return_value.get = AsyncMock(return_value={})
        mock_am.return_value.get_context_fragment = AsyncMock(return_value="")
        conv_svc = AsyncMock()
        conv_svc.get_context = AsyncMock(return_value={"turns": [], "summary": None})
        mock_conv.return_value = conv_svc

        orchestrator = ChatOrchestrator()
        response = await orchestrator.handle(
            request, mock_db, mock_redis, uuid.uuid4(), uuid.uuid4()
        )

    # Debe retornar el fallback de error sin propagar la excepción
    assert response.status == "error"
    assert response.message is not None and len(response.message) > 0
    # Sub-agente NO fue invocado
    mock_executor_instance.execute.assert_not_called()


@pytest.mark.asyncio
async def test_ceo_task_is_always_awaited_on_ceo_failure(
    mock_db: AsyncMock, mock_redis: AsyncMock
) -> None:
    """Garantía de no-tarea-huérfana: el ceo_task es awaited incluso cuando el CEO falla.

    Usa un evento para detectar si el CEO llegó a ejecutarse y el handle() terminó limpiamente.
    """
    request = _make_request()
    ceo_started = asyncio.Event()
    ceo_raised = asyncio.Event()

    async def slow_failing_ceo(req: AgentRequest) -> AgentResponse:
        ceo_started.set()
        # Introducir un yield para que las cargas de contexto avancen antes del fallo
        await asyncio.sleep(0)
        ceo_raised.set()
        raise RuntimeError("CEO falló después de iniciar")

    with (
        patch(f"{ORCHESTRATOR}.AgentCEO") as mock_ceo_cls,
        patch(f"{ORCHESTRATOR}.TeamPlanExecutor"),
        patch(f"{ORCHESTRATOR}.AgentChat"),
        patch(f"{ORCHESTRATOR}.get_anthropic_async_client"),
        patch(f"{ORCHESTRATOR}.BusinessMemoryService") as mock_bm,
        patch(f"{ORCHESTRATOR}.AgentMemoryService") as mock_am,
        patch(f"{ORCHESTRATOR}.ConversationService") as mock_conv,
    ):
        mock_ceo_cls.return_value.process = slow_failing_ceo
        mock_bm.return_value.get = AsyncMock(return_value={})
        mock_am.return_value.get_context_fragment = AsyncMock(return_value="")
        conv_svc = AsyncMock()
        conv_svc.get_context = AsyncMock(return_value={"turns": [], "summary": None})
        mock_conv.return_value = conv_svc

        orchestrator = ChatOrchestrator()
        response = await orchestrator.handle(
            request, mock_db, mock_redis, uuid.uuid4(), uuid.uuid4()
        )

    # El CEO se ejecutó y su excepción fue capturada (ningún task huérfano pendiente)
    assert ceo_started.is_set(), "El task del CEO debería haber iniciado"
    assert ceo_raised.is_set(), "El CEO debería haber lanzado la excepción"
    assert response.status == "error", "El handle() debe devolver status=error cuando el CEO falla"

    # Dar un ciclo del event loop para verificar que no haya warnings de tasks pendientes
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_no_attachment_meta_has_attachment_false(
    mock_db: AsyncMock, mock_redis: AsyncMock
) -> None:
    """Sin adjunto, attachment_meta tiene has_attachment=False cuando el CEO lo recibe."""
    request = _make_request(with_attachment=False)

    captured_contexts: list[dict] = []

    async def fake_ceo_process(req: AgentRequest) -> AgentResponse:
        captured_contexts.append(dict(req.context))
        return _make_ceo_response(req.request_id)

    with (
        patch(f"{ORCHESTRATOR}.AgentCEO") as mock_ceo_cls,
        patch(f"{ORCHESTRATOR}.TeamPlanExecutor") as mock_executor,
        patch(f"{ORCHESTRATOR}.AgentChat") as mock_chat_cls,
        patch(f"{ORCHESTRATOR}.get_anthropic_async_client"),
        patch(f"{ORCHESTRATOR}.BusinessMemoryService") as mock_bm,
        patch(f"{ORCHESTRATOR}.AgentMemoryService") as mock_am,
        patch(f"{ORCHESTRATOR}.ConversationService") as mock_conv,
    ):
        mock_ceo_cls.return_value.process = fake_ceo_process
        sub_resp = _make_sub_response(request.request_id)
        sub_resp.requires_approval = False
        mock_executor.return_value.execute = AsyncMock(return_value=[sub_resp])
        mock_chat_cls.return_value.generate_response = AsyncMock(
            return_value=("OK.", LLMCall(source="agent_chat", model="claude-sonnet-4-6", input_tokens=10, output_tokens=5))
        )
        mock_bm.return_value.get = AsyncMock(return_value={})
        mock_am.return_value.get_context_fragment = AsyncMock(return_value="")
        conv_svc = AsyncMock()
        conv_svc.get_context = AsyncMock(return_value={"turns": [], "summary": None})
        mock_conv.return_value = conv_svc

        orchestrator = ChatOrchestrator()
        await orchestrator.handle(request, mock_db, mock_redis, uuid.uuid4(), uuid.uuid4())

    assert len(captured_contexts) == 1
    ctx = captured_contexts[0]
    assert "attachment_meta" in ctx
    assert ctx["attachment_meta"]["has_attachment"] is False
