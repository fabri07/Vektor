"""Desde el chat sólo escribe quien puede confirmar (R-1).

Revisión profunda 2026-10-24: `REGISTER_EXPENSE` se auto-ejecuta en el chat y
`_process_agent_action` no miraba el rol, así que un VIEWER que escribía
«gasté 5000 en luz» dejaba el gasto persistido; una regla automática de un
VIEWER también se ejecutaba sola. Ahora, para un rol que no confirma, una
respuesta que escribiría algo se reemplaza por un rechazo claro: no se ejecuta
ni queda pendiente (una pendiente de un VIEWER no la aprobaría nadie: vence a
los 10 minutos y el dueño no tiene dónde verla).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.agents.shared.permisos_chat import MENSAJE_SIN_PERMISO
from app.application.agents.shared.schemas import AgentResponse, RiskLevel
from app.config.settings import get_settings
from app.main import create_app
from app.persistence.db.redis_client import get_redis
from app.persistence.db.session import get_db_session
from app.persistence.models.pending_action import PendingAction
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import ExpenseEntry
from app.tests.conftest import add_business_profile
from app.utils.security import create_access_token

from .conftest import FakeRedis, make_tenant, make_user
from .test_endpoints import TEAM_EXECUTOR, _make_ceo_plan_response, _mock_agent_chat


def _gasto_auto(request_id: str | None = None) -> AgentResponse:
    """La respuesta del agente de gastos: auto-ejecutable, sin aprobación."""
    return AgentResponse(
        request_id=request_id or str(uuid.uuid4()),
        agent_name="agent_cash",
        status="success",
        risk_level=RiskLevel.MEDIUM,
        requires_approval=False,
        result={
            "intent": "record_expense",
            "action_type": "REGISTER_EXPENSE",
            "target_agent": "agent_cash",
            "structured_data": {
                "amount": "5000",
                "category": "UTILITIES",
                "payment_method": "cash",
                "description": "Luz",
            },
            "auto_execute": True,
            "summary": "Gasto registrado: Luz por $5000",
        },
    )


def _venta_para_aprobar() -> AgentResponse:
    return AgentResponse(
        request_id=str(uuid.uuid4()),
        agent_name="agent_income",
        status="requires_approval",
        risk_level=RiskLevel.MEDIUM,
        requires_approval=True,
        result={
            "action_type": "REGISTER_SALE",
            "structured_data": {"amount": "300", "payment_method": "cash"},
            "summary": "Registrar venta por $300",
        },
    )


@pytest_asyncio.fixture
async def negocio(session: AsyncSession) -> Tenant:
    tenant = await make_tenant(session)
    await add_business_profile(session, tenant.tenant_id)
    await session.commit()
    return tenant


@pytest_asyncio.fixture
async def ac(session: AsyncSession) -> AsyncIterator[AsyncClient]:
    app = create_app()
    redis = FakeRedis()

    async def _session() -> AsyncIterator[AsyncSession]:
        yield session

    async def _redis() -> AsyncIterator[FakeRedis]:
        yield redis

    app.dependency_overrides[get_db_session] = _session
    app.dependency_overrides[get_redis] = _redis
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def _headers(session: AsyncSession, tenant: Tenant, rol: str) -> dict[str, str]:
    user = await make_user(
        session,
        tenant.tenant_id,
        role_code=rol,
        email=f"{rol.lower()}+{uuid.uuid4().hex[:6]}@test.com",
    )
    token = create_access_token(
        {"sub": str(user.user_id), "tenant_id": str(tenant.tenant_id), "role_code": rol}
    )
    return {"Authorization": f"Bearer {token}"}


async def _contar(session: AsyncSession, modelo: Any, tenant: Tenant) -> int:
    return int(
        (
            await session.execute(
                select(func.count(modelo.id)).where(modelo.tenant_id == tenant.tenant_id)
            )
        ).scalar_one()
    )


async def _chat(
    ac: AsyncClient, headers: dict[str, str], respuesta: AgentResponse, ruta: str = "chat"
) -> Any:
    with patch("app.api.v1.agent.ChatOrchestrator") as orquestador:
        orquestador.return_value.handle = AsyncMock(return_value=respuesta)
        resp = await ac.post(
            f"/api/v1/agent/{ruta}", json={"message": "gasté 5000 en luz"}, headers=headers
        )
    assert resp.status_code == 200, resp.text
    return resp.json() if ruta == "chat" else resp.text


# ── Capa HTTP: la garantía ───────────────────────────────────────────────────


@pytest.mark.parametrize("rol", ["VIEWER", "ANALYST"])
async def test_un_rol_que_no_confirma_no_registra_el_gasto_ni_lo_deja_pendiente(
    ac: AsyncClient, session: AsyncSession, negocio: Tenant, rol: str
) -> None:
    data = await _chat(ac, await _headers(session, negocio, rol), _gasto_auto())

    assert await _contar(session, ExpenseEntry, negocio) == 0
    assert await _contar(session, PendingAction, negocio) == 0
    assert data["message"] == MENSAJE_SIN_PERMISO
    assert data["result"]["blocked_by_role"] is True
    assert data["result"].get("auto_executed") is not True
    assert data["requires_approval"] is False
    assert data["pending_action_id"] is None


async def test_una_venta_para_aprobar_tampoco_queda_pendiente(
    ac: AsyncClient, session: AsyncSession, negocio: Tenant
) -> None:
    """Las que piden aprobación: una pendiente del VIEWER no la aprobaría nadie."""
    data = await _chat(ac, await _headers(session, negocio, "VIEWER"), _venta_para_aprobar())

    assert await _contar(session, PendingAction, negocio) == 0
    assert data["message"] == MENSAJE_SIN_PERMISO


async def test_un_plan_de_varias_tareas_tampoco_deja_pendientes(
    ac: AsyncClient, session: AsyncSession, negocio: Tenant
) -> None:
    plan = AgentResponse(
        request_id=str(uuid.uuid4()),
        agent_name="agent_ceo",
        status="requires_approval",
        risk_level=RiskLevel.MEDIUM,
        requires_approval=True,
        result={
            "task_responses": [
                {
                    "task_id": "t1",
                    "agent": "agent_income",
                    "action_type": "REGISTER_SALE",
                    "requires_approval": True,
                    "payload": {"amount": "300"},
                },
                {"task_id": "t2", "agent": "agent_analytics", "requires_approval": False},
            ]
        },
    )
    data = await _chat(ac, await _headers(session, negocio, "VIEWER"), plan)

    assert await _contar(session, PendingAction, negocio) == 0
    assert data["message"] == MENSAJE_SIN_PERMISO


@pytest.mark.parametrize("rol", ["OWNER", "ADMIN"])
async def test_quien_confirma_sigue_registrando_el_gasto_al_toque(
    ac: AsyncClient, session: AsyncSession, negocio: Tenant, rol: str
) -> None:
    data = await _chat(ac, await _headers(session, negocio, rol), _gasto_auto())

    assert await _contar(session, ExpenseEntry, negocio) == 1
    assert data["result"]["auto_executed"] is True
    assert "blocked_by_role" not in data["result"]


async def test_al_dueno_la_venta_le_sigue_quedando_para_aprobar(
    ac: AsyncClient, session: AsyncSession, negocio: Tenant
) -> None:
    data = await _chat(ac, await _headers(session, negocio, "OWNER"), _venta_para_aprobar())

    assert await _contar(session, PendingAction, negocio) == 1
    assert data["pending_action_id"]


async def test_una_regla_automatica_no_se_ejecuta_para_un_viewer(
    ac: AsyncClient,
    session: AsyncSession,
    negocio: Tenant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(get_settings(), "ENABLE_AGENT_AUTOMATIONS", True)
    regla = MagicMock(id=uuid.uuid4(), rule_key="r")
    with patch(
        "app.api.v1.agent.find_enabled_rule", new=AsyncMock(return_value=regla)
    ) as buscar:
        await _chat(ac, await _headers(session, negocio, "VIEWER"), _venta_para_aprobar())

    buscar.assert_not_called()
    assert await _contar(session, PendingAction, negocio) == 0


async def test_la_regla_del_dueno_sigue_ejecutando(
    ac: AsyncClient,
    session: AsyncSession,
    negocio: Tenant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(get_settings(), "ENABLE_AGENT_AUTOMATIONS", True)
    regla = MagicMock(id=uuid.uuid4(), rule_key="r")
    with (
        patch("app.api.v1.agent.find_enabled_rule", new=AsyncMock(return_value=regla)) as buscar,
        patch(
            "app.api.v1.agent._execute_local_action", new=AsyncMock(return_value=None)
        ) as ejecutar,
    ):
        data = await _chat(ac, await _headers(session, negocio, "OWNER"), _venta_para_aprobar())

    buscar.assert_called_once()
    ejecutar.assert_called_once()
    assert data["result"]["auto_executed"] is True


async def test_el_chat_en_streaming_tampoco_escribe_para_un_viewer(
    ac: AsyncClient, session: AsyncSession, negocio: Tenant
) -> None:
    texto = await _chat(
        ac, await _headers(session, negocio, "VIEWER"), _gasto_auto(), ruta="chat/stream"
    )
    assert "[DONE]" in texto
    assert "solo lectura" in texto
    assert await _contar(session, ExpenseEntry, negocio) == 0
    assert await _contar(session, PendingAction, negocio) == 0


# ── Orquestador: decide antes de narrar ──────────────────────────────────────


async def test_el_orquestador_no_narra_un_gasto_que_no_hizo_y_lo_recuerda_bien(
    ac: AsyncClient, session: AsyncSession, negocio: Tenant
) -> None:
    """Sin mockear el orquestador: AgentChat no corre (no se gasta el LLM ni se
    escribe «registré tu gasto») y el turno guardado es el rechazo."""
    sub = AsyncMock()
    sub.process = AsyncMock(side_effect=lambda req, **_: _gasto_auto(req.request_id))
    agent_chat = _mock_agent_chat()
    orq = "app.application.services.chat_orchestrator"
    with (
        patch(f"{orq}.AgentCEO") as ceo,
        patch(f"{TEAM_EXECUTOR}.get_sub_agent", return_value=sub),
        patch(f"{orq}.ConversationService") as conv,
        patch(f"{orq}.get_anthropic_async_client"),
        patch(f"{orq}.AgentChat", return_value=agent_chat),
    ):
        ceo.return_value.process = AsyncMock(
            side_effect=lambda req: _make_ceo_plan_response(
                req.request_id, "ingresar_gasto", "agent_expense", "REGISTER_EXPENSE"
            )
        )
        conv.return_value.get_context = AsyncMock(return_value={"turns": [], "summary": None})
        conv.return_value.add_turn = AsyncMock()
        conv.return_value.persist = AsyncMock()
        resp = await ac.post(
            "/api/v1/agent/chat",
            json={"message": "gasté 5000 en luz", "conversation_id": str(uuid.uuid4())},
            headers=await _headers(session, negocio, "VIEWER"),
        )

    assert resp.status_code == 200, resp.text
    assert resp.json()["message"] == MENSAJE_SIN_PERMISO
    agent_chat.generate_response.assert_not_called()
    turnos = [str(c) for c in conv.return_value.add_turn.call_args_list]
    assert any(MENSAJE_SIN_PERMISO in t for t in turnos), turnos
    assert not any("registr" in t.lower() and "solo lectura" not in t for t in turnos), turnos
    assert await _contar(session, ExpenseEntry, negocio) == 0


async def test_un_plan_con_un_gasto_adentro_se_rechaza_aunque_arriba_no_pida_aprobacion(
    ac: AsyncClient, session: AsyncSession, negocio: Tenant
) -> None:
    """La tarea escribe aunque el plan no lo diga arriba: también es rechazo."""
    gasto = _gasto_auto().result
    plan = AgentResponse(
        request_id=str(uuid.uuid4()),
        agent_name="agent_ceo",
        status="success",
        risk_level=RiskLevel.LOW,
        requires_approval=False,
        result={"task_responses": [{"task_id": "t1", "requires_approval": False, **gasto}]},
    )
    data = await _chat(ac, await _headers(session, negocio, "VIEWER"), plan)

    assert data["message"] == MENSAJE_SIN_PERMISO
    assert await _contar(session, ExpenseEntry, negocio) == 0
