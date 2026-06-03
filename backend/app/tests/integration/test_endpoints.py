"""Tests de integración para endpoints /agent/chat, /confirm, /cancel.

Usa SQLite in-memory + FakeRedis — sin infraestructura externa.
Los tests mockean AgentCEO.process para evitar llamadas reales al LLM.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, patch

import anthropic
import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.agents.shared.event_bus import EventBus
from app.application.agents.shared.schemas import AgentResponse, RiskLevel
from app.integrations.mcp.exceptions import McpToolAuthError
from app.main import create_app
from app.persistence.db.redis_client import get_redis
from app.persistence.db.session import get_db_session
from app.persistence.models.pending_action import PendingAction
from app.persistence.models.transaction import ExpenseEntry, SaleEntry

from .conftest import FakeRedis, make_auth_headers, make_tenant, make_user

# ── FakeRedis extendida con contador de rate limit ────────────────────────────


class FakeRedisCounter(FakeRedis):
    """FakeRedis con contador persistente para simular rate limiting."""


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def tenant_and_user(session: AsyncSession):
    tenant = await make_tenant(session)
    user = await make_user(session, tenant.tenant_id)
    return tenant, user


@pytest_asyncio.fixture
async def auth_client(session: AsyncSession, tenant_and_user):
    tenant, user = tenant_and_user
    headers = make_auth_headers(user.user_id, tenant.tenant_id)
    fake_redis = FakeRedisCounter()

    app = create_app()

    async def _override_session():
        yield session

    async def _override_redis():
        yield fake_redis

    app.dependency_overrides[get_db_session] = _override_session
    app.dependency_overrides[get_redis] = _override_redis

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac, headers, tenant, user, fake_redis


@pytest_asyncio.fixture
async def auth_client_second_tenant(session: AsyncSession):
    """Cliente para un segundo tenant (para tests de aislamiento)."""
    tenant2 = await make_tenant(session, legal_name="Otro Negocio", display_name="Otro Negocio")
    user2 = await make_user(session, tenant2.tenant_id)
    headers2 = make_auth_headers(user2.user_id, tenant2.tenant_id)
    fake_redis = FakeRedisCounter()

    app = create_app()

    async def _override_session():
        yield session

    async def _override_redis():
        yield fake_redis

    app.dependency_overrides[get_db_session] = _override_session
    app.dependency_overrides[get_redis] = _override_redis

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac, headers2, tenant2, user2


# ── Helper: AgentResponse mock ────────────────────────────────────────────────

TEAM_EXECUTOR = "app.application.services.team_plan_executor"


def _make_ceo_plan_response(
    request_id: str,
    intent: str,
    agent: str,
    action_type: str,
    entities: dict[str, Any] | None = None,
) -> AgentResponse:
    """Respuesta del CEO con un plan single-task (Stage 3 format)."""
    plan_dict = {
        "plan_id": str(uuid.uuid4()),
        "intent": intent,
        "tasks": [
            {
                "task_id": str(uuid.uuid4()),
                "agent": agent,
                "action_type": action_type,
                "entities": entities or {},
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
        result={
            "intent": intent,
            "action_type": action_type,
            "target_agent": agent,
            "plan": plan_dict,
        },
    )


def _mock_requires_approval_response(request_id: str) -> AgentResponse:
    return AgentResponse(
        request_id=request_id,
        agent_name="agent_ceo",
        status="requires_approval",
        risk_level=RiskLevel.MEDIUM,
        requires_approval=True,
        result={
            "intent": "record_sale",
            "action_type": "REGISTER_SALE",
            "target_agent": "agent_cash",
            "entities": {"amount": 500},
        },
    )


def _mock_low_risk_response(request_id: str) -> AgentResponse:
    return AgentResponse(
        request_id=request_id,
        agent_name="agent_ceo",
        status="success",
        risk_level=RiskLevel.LOW,
        requires_approval=False,
        result={
            "intent": "ask_platform_help",
            "action_type": "ANSWER_HELP_REQUEST",
            "target_agent": "agent_helper",
            "entities": {},
        },
    )


def _mock_anthropic_overload_error() -> anthropic.InternalServerError:
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx.Response(status_code=529, request=request)
    body = {
        "type": "error",
        "error": {"type": "overloaded_error", "message": "Overloaded"},
        "request_id": "req_test_overload",
    }
    return anthropic.InternalServerError(
        "Error code: 529",
        response=response,
        body=body,
    )


# ── Tests ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_medium_action_creates_pending_not_persists(
    auth_client, session: AsyncSession
) -> None:
    """Un REGISTER_SALE (MEDIUM) debe crear pending_action en DB, NO SaleEntry."""
    ac, headers, tenant, user, _ = auth_client

    sub_mock = AsyncMock()
    sub_mock.process = AsyncMock(
        side_effect=lambda req, **_: _mock_requires_approval_response(req.request_id)
    )
    orchestrator = "app.application.services.chat_orchestrator"
    with (
        patch(f"{orchestrator}.AgentCEO") as mock_ceo,
        patch(f"{TEAM_EXECUTOR}.get_sub_agent", return_value=sub_mock),
        patch(f"{orchestrator}.ConversationService") as mock_conv,
        patch(f"{orchestrator}.get_anthropic_async_client"),
    ):
        mock_ceo.return_value.process = AsyncMock(
            side_effect=lambda req: _make_ceo_plan_response(
                req.request_id, "ingresar_venta", "agent_income", "REGISTER_SALE", {"amount": 500}
            )
        )
        mock_conv.return_value.get_context = AsyncMock(return_value={"turns": [], "summary": None})
        mock_conv.return_value.add_turn = AsyncMock()
        mock_conv.return_value.persist = AsyncMock()

        resp = await ac.post(
            "/api/v1/agent/chat",
            json={"message": "vendí 500 pesos"},
            headers=headers,
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "requires_approval"
    assert data["requires_approval"] is True
    assert data["pending_action_id"] is not None

    # Verificar pending_action en DB
    result = await session.execute(
        select(PendingAction).where(PendingAction.tenant_id == tenant.tenant_id)
    )
    pending = result.scalar_one_or_none()
    assert pending is not None
    assert pending.action_type == "REGISTER_SALE"
    assert pending.status == "PENDING"

    # Verificar que NO se creó SaleEntry
    sales = await session.execute(select(SaleEntry).where(SaleEntry.tenant_id == tenant.tenant_id))
    assert sales.scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_expense_can_auto_execute_from_chat(auth_client, session: AsyncSession) -> None:
    """Un REGISTER_EXPENSE marcado para auto-ejecución debe persistirse sin pending visible."""
    ac, headers, tenant, user, _ = auth_client

    sub_mock = AsyncMock()
    sub_mock.process = AsyncMock(
        side_effect=lambda req, **_: AgentResponse(
            request_id=req.request_id,
            agent_name="agent_cash",
            status="success",
            risk_level=RiskLevel.MEDIUM,
            requires_approval=False,
            result={
                "intent": "record_expense",
                "action_type": "REGISTER_EXPENSE",
                "target_agent": "agent_cash",
                "structured_data": {
                    "amount": "450000",
                    "category": "RENT",
                    "transaction_date": "2026-04-21",
                    "payment_method": "transfer",
                    "description": "Alquiler",
                },
                "auto_execute": True,
                "summary": "Gasto registrado: Alquiler por $450000",
            },
        )
    )
    orchestrator = "app.application.services.chat_orchestrator"
    with (
        patch(f"{orchestrator}.AgentCEO") as mock_ceo,
        patch(f"{TEAM_EXECUTOR}.get_sub_agent", return_value=sub_mock),
        patch(f"{orchestrator}.ConversationService") as mock_conv,
        patch(f"{orchestrator}.get_anthropic_async_client"),
    ):
        mock_ceo.return_value.process = AsyncMock(
            side_effect=lambda req: _make_ceo_plan_response(
                req.request_id, "ingresar_gasto", "agent_expense", "REGISTER_EXPENSE"
            )
        )
        mock_conv.return_value.get_context = AsyncMock(return_value={"turns": [], "summary": None})
        mock_conv.return_value.add_turn = AsyncMock()
        mock_conv.return_value.persist = AsyncMock()

        resp = await ac.post(
            "/api/v1/agent/chat",
            json={"message": "Pagué alquiler $450.000"},
            headers=headers,
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "success"
    assert data["pending_action_id"] is None

    expense = await session.execute(
        select(ExpenseEntry).where(ExpenseEntry.tenant_id == tenant.tenant_id)
    )
    saved = expense.scalar_one_or_none()
    assert saved is not None
    assert float(saved.amount) == 450000.0
    assert saved.category == "RENT"


@pytest.mark.asyncio
async def test_auto_execute_failure_returns_error_status(
    auth_client, session: AsyncSession
) -> None:
    """Si falla la auto-ejecución local, el chat no debe reportar success."""
    ac, headers, tenant, user, _ = auth_client

    with (
        patch("app.api.v1.agent.ChatOrchestrator") as mock_orchestrator,
        patch(
            "app.api.v1.agent.execute_pending_action",
            new=AsyncMock(side_effect=RuntimeError("simulated write failure")),
        ),
    ):
        mock_orchestrator.return_value.handle = AsyncMock(
            return_value=AgentResponse(
                request_id=str(uuid.uuid4()),
                agent_name="agent_cash",
                status="success",
                risk_level=RiskLevel.MEDIUM,
                requires_approval=False,
                result={
                    "intent": "record_expense",
                    "action_type": "REGISTER_EXPENSE",
                    "target_agent": "agent_cash",
                    "structured_data": {
                        "amount": "1000",
                        "category": "OTHER",
                        "payment_method": "cash",
                        "description": "Test",
                    },
                },
            )
        )

        resp = await ac.post(
            "/api/v1/agent/chat",
            json={"message": "pagué 1000"},
            headers=headers,
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "error"
    assert data["result"]["execution_status"] == "FAILED"
    assert data["result"]["auto_executed"] is False


@pytest.mark.asyncio
async def test_confirm_succeeds_and_marks_executed(auth_client, session: AsyncSession) -> None:
    """Confirmar una pending_action → status=APPROVED, executed_at seteado."""
    ac, headers, tenant, user, _ = auth_client

    # Crear pending_action directamente en DB
    # payment_method ahora es obligatorio (save_sale rechaza ventas sin método).
    action = PendingAction(
        tenant_id=tenant.tenant_id,
        user_id=user.user_id,
        action_type="REGISTER_SALE",
        payload={"amount": 300, "payment_method": "cash"},
        risk_level="MEDIUM",
        status="PENDING",
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
    )
    session.add(action)
    await session.commit()

    with patch.object(EventBus, "emit"):
        resp = await ac.post(f"/api/v1/agent/confirm/{action.id}", headers=headers)

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "confirmed"
    assert data["action_type"] == "REGISTER_SALE"

    await session.refresh(action)
    assert action.status == "APPROVED"
    assert action.executed_at is not None


@pytest.mark.asyncio
async def test_confirm_same_id_twice_fails(auth_client, session: AsyncSession) -> None:
    """Intentar confirmar la misma pending_action dos veces → 404 en el segundo intento."""
    ac, headers, tenant, user, _ = auth_client

    action = PendingAction(
        tenant_id=tenant.tenant_id,
        user_id=user.user_id,
        action_type="REGISTER_EXPENSE",
        payload={"amount": 100},
        risk_level="MEDIUM",
        status="PENDING",
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
    )
    session.add(action)
    await session.commit()

    resp1 = await ac.post(f"/api/v1/agent/confirm/{action.id}", headers=headers)
    assert resp1.status_code == 200

    resp2 = await ac.post(f"/api/v1/agent/confirm/{action.id}", headers=headers)
    assert resp2.status_code == 404


@pytest.mark.asyncio
async def test_confirm_expired_fails(auth_client, session: AsyncSession) -> None:
    """Confirmar una pending_action vencida → 410 Gone."""
    ac, headers, tenant, user, _ = auth_client

    action = PendingAction(
        tenant_id=tenant.tenant_id,
        user_id=user.user_id,
        action_type="REGISTER_SALE",
        payload={"amount": 200},
        risk_level="MEDIUM",
        status="PENDING",
        expires_at=datetime.now(UTC) - timedelta(minutes=1),  # ya venció
    )
    session.add(action)
    await session.commit()

    resp = await ac.post(f"/api/v1/agent/confirm/{action.id}", headers=headers)

    assert resp.status_code == 410
    assert "venció" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_confirm_external_action_requires_reconnect_on_mcp_auth_error(
    auth_client, session: AsyncSession
) -> None:
    """Una acción externa debe quedar en REQUIRES_RECONNECT cuando el MCP pide auth."""
    ac, headers, tenant, user, _ = auth_client

    action = PendingAction(
        tenant_id=tenant.tenant_id,
        user_id=user.user_id,
        action_type="CREATE_CALENDAR_EVENT",
        payload={"mode": "mcp", "summary": "Reunión"},
        risk_level="MEDIUM",
        status="PENDING",
        external_system="GOOGLE_CALENDAR",
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
    )
    session.add(action)
    await session.commit()

    with patch(
        "app.api.v1.agent.execute_pending_action",
        new=AsyncMock(
            side_effect=McpToolAuthError(
                "Error de autenticación Google: not_connected",
                reason="not_connected",
            )
        ),
    ):
        resp = await ac.post(f"/api/v1/agent/confirm/{action.id}", headers=headers)

    assert resp.status_code == 200
    assert resp.json()["execution_status"] == "REQUIRES_RECONNECT"
    assert resp.json()["failure_code"] == "not_connected"

    await session.refresh(action)
    assert action.status == "APPROVED"
    assert action.execution_status == "REQUIRES_RECONNECT"
    assert action.failure_code == "not_connected"


@pytest.mark.asyncio
async def test_rate_limit_429(auth_client):
    """51 requests al chat → el 51° retorna 429."""
    ac, headers, tenant, user, fake_redis = auth_client

    # Precargar el contador a 50 para no hacer 51 llamadas reales al LLM
    rate_key = f"rate:chat:{tenant.tenant_id}:{__import__('datetime').date.today()}"
    fake_redis._store[rate_key] = ("50", None)

    resp = await ac.post(
        "/api/v1/agent/chat",
        json={"message": "mensaje 51"},
        headers=headers,
    )

    assert resp.status_code == 429
    assert "50 mensajes" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_chat_error_response_does_not_increment_rate_limit(auth_client):
    """Errores controlados del orquestador no consumen cuota diaria."""
    ac, headers, tenant, user, fake_redis = auth_client
    rate_key = f"rate:chat:{tenant.tenant_id}:{__import__('datetime').date.today()}"

    with patch("app.api.v1.agent.ChatOrchestrator") as mock_orchestrator:
        mock_orchestrator.return_value.handle = AsyncMock(
            return_value=AgentResponse(
                request_id=str(uuid.uuid4()),
                agent_name="agent_ceo",
                status="error",
                risk_level=RiskLevel.LOW,
                requires_approval=False,
                result={"summary": "CEO classification failed"},
                message="No pude clasificar tu mensaje.",
            )
        )
        resp = await ac.post(
            "/api/v1/agent/chat",
            json={"message": "hola"},
            headers=headers,
        )

    assert resp.status_code == 200
    assert resp.json()["status"] == "error"
    assert await fake_redis.get(rate_key) is None


@pytest.mark.asyncio
async def test_chat_overloaded_provider_returns_safe_503(auth_client):
    """Un 529 de Anthropic debe degradar a 503 sin filtrar el error crudo."""
    ac, headers, tenant, user, _ = auth_client

    with patch("app.api.v1.agent.ChatOrchestrator") as mock_orchestrator:
        mock_orchestrator.return_value.handle = AsyncMock(
            side_effect=_mock_anthropic_overload_error()
        )
        resp = await ac.post(
            "/api/v1/agent/chat",
            json={"message": "¿Cómo viene mi flujo de caja?"},
            headers=headers,
        )

    assert resp.status_code == 503
    assert (
        resp.json()["detail"]
        == "El servicio de IA está saturado temporalmente. Intenta de nuevo en unos segundos."
    )
    assert "529" not in resp.text
    assert "Overloaded" not in resp.text


@pytest.mark.asyncio
async def test_chat_stream_overloaded_provider_returns_safe_sse_error(auth_client):
    """El stream debe emitir un error seguro y finalizar cuando Anthropic devuelve 529."""
    ac, headers, tenant, user, _ = auth_client

    with patch("app.api.v1.agent.ChatOrchestrator") as mock_orchestrator:
        mock_orchestrator.return_value.handle = AsyncMock(
            side_effect=_mock_anthropic_overload_error()
        )
        resp = await ac.post(
            "/api/v1/agent/chat/stream",
            json={"message": "Necesito ayuda con ventas"},
            headers=headers,
        )

    assert resp.status_code == 200
    assert "Analizando tu mensaje..." in resp.text
    assert (
        "El servicio de IA está saturado temporalmente. Intenta de nuevo en unos segundos."
        in resp.text
    )
    assert '"code": 503' in resp.text
    assert "[DONE]" in resp.text
    assert "529" not in resp.text
    assert "Overloaded" not in resp.text


@pytest.mark.asyncio
async def test_cross_tenant_pending_action_invisible(auth_client, session: AsyncSession) -> None:
    """Tenant B no puede confirmar ni cancelar una pending_action de Tenant A."""
    ac_a, headers_a, tenant_a, user_a, _ = auth_client

    # Crear un segundo tenant/usuario directamente
    tenant_b = await make_tenant(session, legal_name="Tenant B", display_name="Tenant B")
    user_b = await make_user(session, tenant_b.tenant_id)
    headers_b = make_auth_headers(user_b.user_id, tenant_b.tenant_id)

    fake_redis_b = FakeRedisCounter()
    app = create_app()

    async def _override_session():
        yield session

    async def _override_redis():
        yield fake_redis_b

    app.dependency_overrides[get_db_session] = _override_session
    app.dependency_overrides[get_redis] = _override_redis

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac_b:
        # Crear pending_action de tenant A
        action = PendingAction(
            tenant_id=tenant_a.tenant_id,
            user_id=user_a.user_id,
            action_type="REGISTER_SALE",
            payload={"amount": 500},
            risk_level="MEDIUM",
            status="PENDING",
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
        )
        session.add(action)
        await session.commit()

        # Tenant B intenta confirmar → debe recibir 404
        resp = await ac_b.post(f"/api/v1/agent/confirm/{action.id}", headers=headers_b)
        assert resp.status_code == 404


# ── WF-02 + Cancel ────────────────────────────────────────────────────────────


def _mock_clarification_response(request_id: str) -> AgentResponse:
    return AgentResponse(
        request_id=request_id,
        agent_name="agent_ceo",
        status="requires_clarification",
        risk_level=RiskLevel.LOW,
        requires_approval=False,
        result={"question": "¿Fue al contado o en cuenta corriente?", "target_agent": "agent_cash"},
    )


@pytest.mark.asyncio
async def test_wf02_requires_clarification_no_pending(auth_client, session: AsyncSession) -> None:
    """WF-02: requires_clarification no crea PendingAction en DB."""
    ac, headers, tenant, user, _ = auth_client

    orchestrator = "app.application.services.chat_orchestrator"
    sub_mock = AsyncMock()
    sub_mock.process = AsyncMock(
        side_effect=lambda req, **_: _mock_clarification_response(req.request_id)
    )
    with (
        patch(f"{orchestrator}.AgentCEO") as mock_ceo,
        patch(f"{TEAM_EXECUTOR}.get_sub_agent", return_value=sub_mock),
        patch(f"{orchestrator}.ConversationService") as mock_conv,
        patch(f"{orchestrator}.get_anthropic_async_client") as mock_anthropic_factory,
    ):
        mock_ceo.return_value.process = AsyncMock(
            side_effect=lambda req: _make_ceo_plan_response(
                req.request_id, "ingresar_venta", "agent_income", "REGISTER_SALE"
            )
        )
        mock_conv.return_value.get_context = AsyncMock(return_value={"turns": [], "summary": None})
        mock_conv.return_value.add_turn = AsyncMock()
        mock_conv.return_value.persist = AsyncMock()
        mock_client = AsyncMock()
        mock_client.messages.create = AsyncMock(
            return_value=AsyncMock(
                content=[AsyncMock(text="¿Fue al contado o en cuenta corriente?")]
            )
        )
        mock_anthropic_factory.return_value = mock_client

        resp = await ac.post(
            "/api/v1/agent/chat",
            json={"message": "vendí 50 mil hoy"},
            headers=headers,
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "requires_clarification"
    assert data.get("pending_action_id") is None

    result = await session.execute(
        select(PendingAction).where(PendingAction.tenant_id == tenant.tenant_id)
    )
    assert result.scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_cancel_succeeds(auth_client, session: AsyncSession) -> None:
    """Cancelar una pending_action → status=REJECTED."""
    ac, headers, tenant, user, _ = auth_client

    action = PendingAction(
        tenant_id=tenant.tenant_id,
        user_id=user.user_id,
        action_type="REGISTER_SALE",
        payload={"amount": 300},
        risk_level="MEDIUM",
        status="PENDING",
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
    )
    session.add(action)
    await session.commit()

    resp = await ac.post(f"/api/v1/agent/cancel/{action.id}", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelled"

    await session.refresh(action)
    assert action.status == "REJECTED"


# ── /confirm/group y /cancel/group ──────────────────────────────────────────


def _make_group_action(
    *,
    tenant_id,
    user_id,
    group_id: str,
    position: int,
    action_type: str = "REGISTER_EXPENSE",
    payload: dict[str, Any] | None = None,
) -> PendingAction:
    return PendingAction(
        tenant_id=tenant_id,
        user_id=user_id,
        action_type=action_type,
        payload=payload or {"amount": 100},
        risk_level="MEDIUM",
        status="PENDING",
        approval_group_id=group_id,
        group_execution_status="PENDING",
        group_position=position,
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
    )


@pytest.mark.asyncio
async def test_confirm_group_success_executes_all_in_order(
    auth_client, session: AsyncSession
) -> None:
    """Grupo de 2 PAs locales que terminan bien → group_execution_status=SUCCEEDED."""
    ac, headers, tenant, user, _ = auth_client
    group_id = str(uuid.uuid4())

    a1 = _make_group_action(
        tenant_id=tenant.tenant_id,
        user_id=user.user_id,
        group_id=group_id,
        position=0,
        action_type="REGISTER_PURCHASE",
        payload={"amount": 5000, "supplier": "Coca"},
    )
    a2 = _make_group_action(
        tenant_id=tenant.tenant_id,
        user_id=user.user_id,
        group_id=group_id,
        position=1,
        action_type="REGISTER_EXPENSE",
        payload={"amount": 5000, "payment_method": "cash"},
    )
    session.add_all([a1, a2])
    await session.commit()

    with patch(
        "app.api.v1.agent._execute_local_action",
        new=AsyncMock(return_value=None),
    ):
        # _execute_local_action no setea SUCCEEDED por sí mismo en tests; lo emulamos
        async def _execute_and_mark(action, **_kw):
            action.execution_status = "SUCCEEDED"
            return None

        with patch(
            "app.api.v1.agent._execute_local_action",
            new=AsyncMock(side_effect=_execute_and_mark),
        ):
            resp = await ac.post(f"/api/v1/agent/confirm/group/{group_id}", headers=headers)

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["group_execution_status"] == "SUCCEEDED"
    assert len(data["tasks"]) == 2
    assert all(t["execution_status"] == "SUCCEEDED" for t in data["tasks"])

    await session.refresh(a1)
    await session.refresh(a2)
    assert a1.execution_status == "SUCCEEDED"
    assert a2.execution_status == "SUCCEEDED"
    assert a1.group_execution_status == "SUCCEEDED"
    assert a2.group_execution_status == "SUCCEEDED"


@pytest.mark.asyncio
async def test_confirm_group_aborts_chain_after_first_failure(
    auth_client, session: AsyncSession
) -> None:
    """Si la primera task falla, la segunda queda FAILED sin ejecutarse (abort-all)."""
    ac, headers, tenant, user, _ = auth_client
    group_id = str(uuid.uuid4())

    a1 = _make_group_action(
        tenant_id=tenant.tenant_id,
        user_id=user.user_id,
        group_id=group_id,
        position=0,
        action_type="REGISTER_PURCHASE",
        payload={"amount": 5000},
    )
    a2 = _make_group_action(
        tenant_id=tenant.tenant_id,
        user_id=user.user_id,
        group_id=group_id,
        position=1,
        action_type="REGISTER_EXPENSE",
        payload={"amount": 5000, "payment_method": "cash"},
    )
    session.add_all([a1, a2])
    await session.commit()

    call_log: list[str] = []

    async def fail_first(action, **_kw):
        call_log.append(str(action.id))
        action.execution_status = "FAILED"
        return {"execution_status": "FAILED"}

    with patch(
        "app.api.v1.agent._execute_local_action", new=AsyncMock(side_effect=fail_first)
    ) as mock_exec:
        resp = await ac.post(f"/api/v1/agent/confirm/group/{group_id}", headers=headers)

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["group_execution_status"] == "PARTIAL_FAILED"
    # _execute_local_action sólo debe haberse llamado una vez (la primera task)
    assert mock_exec.await_count == 1
    assert str(a1.id) in call_log
    assert str(a2.id) not in call_log

    await session.refresh(a1)
    await session.refresh(a2)
    assert a1.execution_status == "FAILED"
    # a2 queda FAILED sin ejecutar
    assert a2.execution_status == "FAILED"
    assert a2.failure_message == "upstream_task_failed"
    assert a2.group_execution_status == "PARTIAL_FAILED"


@pytest.mark.asyncio
async def test_cancel_group_rejects_all(auth_client, session: AsyncSession) -> None:
    """Cancelar grupo → todas las PAs PENDING quedan REJECTED."""
    ac, headers, tenant, user, _ = auth_client
    group_id = str(uuid.uuid4())

    a1 = _make_group_action(
        tenant_id=tenant.tenant_id,
        user_id=user.user_id,
        group_id=group_id,
        position=0,
    )
    a2 = _make_group_action(
        tenant_id=tenant.tenant_id,
        user_id=user.user_id,
        group_id=group_id,
        position=1,
    )
    session.add_all([a1, a2])
    await session.commit()

    resp = await ac.post(f"/api/v1/agent/cancel/group/{group_id}", headers=headers)

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "cancelled"
    assert data["cancelled_count"] == 2

    await session.refresh(a1)
    await session.refresh(a2)
    assert a1.status == "REJECTED"
    assert a2.status == "REJECTED"


@pytest.mark.asyncio
async def test_confirm_group_not_found_404(auth_client):
    """Grupo inexistente → 404."""
    ac, headers, _, _, _ = auth_client
    resp = await ac.post(f"/api/v1/agent/confirm/group/{uuid.uuid4()}", headers=headers)
    assert resp.status_code == 404
