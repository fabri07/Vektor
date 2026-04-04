"""Tests de integración para endpoints /agent/chat, /confirm, /cancel.

Usa SQLite in-memory + FakeRedis — sin infraestructura externa.
Los tests mockean AgentCEO.process para evitar llamadas reales al LLM.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.agents.shared.event_bus import EventBus
from app.application.agents.shared.schemas import AgentResponse, RiskLevel
from app.main import create_app
from app.persistence.db.redis_client import get_redis
from app.persistence.db.session import get_db_session
from app.persistence.models.pending_action import PendingAction
from app.persistence.models.transaction import SaleEntry

from .conftest import FakeRedis, make_auth_headers, make_tenant, make_user


# ── FakeRedis extendida con contador de rate limit ────────────────────────────


class FakeRedisCounter(FakeRedis):
    """FakeRedis con contador persistente para simular rate limiting."""

    def __init__(self) -> None:
        super().__init__()
        self._counters: dict[str, int] = {}

    async def incr(self, key: str) -> int:
        self._counters[key] = self._counters.get(key, 0) + 1
        return self._counters[key]

    async def expireat(self, key: str, when: Any) -> bool:
        return True


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

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
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

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac, headers2, tenant2, user2


# ── Helper: AgentResponse mock ────────────────────────────────────────────────


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


# ── Tests ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_medium_action_creates_pending_not_persists(auth_client, session: AsyncSession):
    """Un REGISTER_SALE (MEDIUM) debe crear pending_action en DB, NO SaleEntry."""
    ac, headers, tenant, user, _ = auth_client

    sub_mock = AsyncMock()
    sub_mock.process = AsyncMock(
        side_effect=lambda req: _mock_requires_approval_response(req.request_id)
    )
    with patch(
        "app.api.v1.agent.AgentCEO.process",
        new=AsyncMock(side_effect=lambda req: _mock_requires_approval_response(req.request_id)),
    ), patch("app.api.v1.agent._get_sub_agent", return_value=sub_mock):
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
    sales = await session.execute(
        select(SaleEntry).where(SaleEntry.tenant_id == tenant.tenant_id)
    )
    assert sales.scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_confirm_succeeds_and_marks_executed(auth_client, session: AsyncSession):
    """Confirmar una pending_action → status=APPROVED, executed_at seteado."""
    ac, headers, tenant, user, _ = auth_client

    # Crear pending_action directamente en DB
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
async def test_confirm_same_id_twice_fails(auth_client, session: AsyncSession):
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
async def test_confirm_expired_fails(auth_client, session: AsyncSession):
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
async def test_rate_limit_429(auth_client):
    """51 requests al chat → el 51° retorna 429."""
    ac, headers, tenant, user, fake_redis = auth_client

    # Precargar el contador a 50 para no hacer 51 llamadas reales al LLM
    rate_key = f"rate:chat:{tenant.tenant_id}:{__import__('datetime').date.today()}"
    fake_redis._counters[rate_key] = 50

    with patch(
        "app.api.v1.agent.AgentCEO.process",
        new=AsyncMock(side_effect=lambda req: _mock_low_risk_response(req.request_id)),
    ):
        resp = await ac.post(
            "/api/v1/agent/chat",
            json={"message": "mensaje 51"},
            headers=headers,
        )

    assert resp.status_code == 429
    assert "50 mensajes" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_cross_tenant_pending_action_invisible(
    auth_client, session: AsyncSession
):
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

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac_b:
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
        resp = await ac_b.post(
            f"/api/v1/agent/confirm/{action.id}", headers=headers_b
        )
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
async def test_wf02_requires_clarification_no_pending(auth_client, session: AsyncSession):
    """WF-02: requires_clarification no crea PendingAction en DB."""
    ac, headers, tenant, user, _ = auth_client

    with patch(
        "app.api.v1.agent.AgentCEO.process",
        new=AsyncMock(side_effect=lambda req: _mock_clarification_response(req.request_id)),
    ), patch("app.api.v1.agent._get_sub_agent", return_value=None):
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
async def test_cancel_succeeds(auth_client, session: AsyncSession):
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
