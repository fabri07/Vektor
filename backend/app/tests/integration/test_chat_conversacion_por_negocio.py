"""Una conversación sólo la lee y la escribe su negocio (X6-2).

Revisión profunda 2026-10-26: el `conversation_id` lo manda el cliente y
`ConversationService` leía `conv:{id}` de Redis o la fila de
`agent_conversation_context` sin mirar el tenant, y `persist()` pisaba los
turnos de una fila existente. Un usuario del negocio B que mandaba el
`conversation_id` de A recibía los turnos de A como contexto del agente y
escribía los suyos en la conversación de A. Ahora la clave de Redis lleva el
tenant y una fila de otro negocio se trata como inexistente: no se lee ni se
pisa (el turno de B queda sólo en su propia clave de Redis).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.agent import ChatRequest, _try_nl_confirmation
from app.application.agents.shared.schemas import AgentResponse, RiskLevel
from app.application.services.chat_orchestrator import ChatOrchestrator
from app.application.services.conversation_service import ConversationService
from app.main import create_app
from app.persistence.db.redis_client import get_redis
from app.persistence.db.session import get_db_session
from app.persistence.models.conversation_context import AgentConversationContext
from app.persistence.models.tenant import Tenant
from app.persistence.models.user import User
from app.tests.conftest import add_business_profile
from app.utils.security import create_access_token

from .conftest import FakeRedis, make_tenant, make_user
from .test_endpoints import TEAM_EXECUTOR, _make_ceo_plan_response, _mock_agent_chat

_ORQ = "app.application.services.chat_orchestrator"
_SECRETO = "el margen de A es 31 %"


@pytest_asyncio.fixture
async def negocio_a(session: AsyncSession) -> Tenant:
    tenant = await make_tenant(session)
    await add_business_profile(session, tenant.tenant_id)
    await session.commit()
    return tenant


@pytest_asyncio.fixture
async def negocio_b(session: AsyncSession) -> Tenant:
    tenant = await make_tenant(session)
    await add_business_profile(session, tenant.tenant_id)
    await session.commit()
    return tenant


@pytest.fixture
def redis() -> FakeRedis:
    return FakeRedis()


@pytest_asyncio.fixture
async def ac(session: AsyncSession, redis: FakeRedis) -> AsyncIterator[AsyncClient]:
    app = create_app()

    async def _session() -> AsyncIterator[AsyncSession]:
        yield session

    async def _redis() -> AsyncIterator[FakeRedis]:
        yield redis

    app.dependency_overrides[get_db_session] = _session
    app.dependency_overrides[get_redis] = _redis
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def _usuario(session: AsyncSession, tenant: Tenant) -> User:
    user = await make_user(
        session, tenant.tenant_id, email=f"owner+{uuid.uuid4().hex[:6]}@test.com"
    )
    await session.commit()
    return user


def _headers(user: User) -> dict[str, str]:
    token = create_access_token(
        {"sub": str(user.user_id), "tenant_id": str(user.tenant_id), "role_code": "OWNER"}
    )
    return {"Authorization": f"Bearer {token}"}


async def _conversacion_de_a(
    session: AsyncSession, redis: FakeRedis, tenant: Tenant, user: User
) -> str:
    """La conversación de A, en la base y en Redis (también con la clave vieja)."""
    conv_id = uuid.uuid4()
    turnos = [
        {"role": "user", "content": "¿cuánto gano?"},
        {"role": "assistant", "content": _SECRETO},
    ]
    session.add(
        AgentConversationContext(
            conversation_id=conv_id,
            tenant_id=tenant.tenant_id,
            user_id=user.user_id,
            turns=turnos,
            summary="resumen de A",
        )
    )
    await session.commit()
    datos = json.dumps({"turns": turnos, "summary": "resumen de A"})
    await redis.setex(f"conv:{tenant.tenant_id}:{conv_id}", 86400, datos)
    # Una clave del formato anterior, viva todavía en Redis: no se lee más.
    await redis.setex(f"conv:{conv_id}", 86400, datos)
    return str(conv_id)


def _respuesta_de_consulta(request_id: str) -> AgentResponse:
    return AgentResponse(
        request_id=request_id,
        agent_name="agent_analytics",
        status="success",
        risk_level=RiskLevel.LOW,
        requires_approval=False,
        result={"intent": "consultar_ventas", "summary": "Ventas del mes"},
    )


async def _chat(
    ac: AsyncClient, user: User, conv_id: str, ruta: str = "chat"
) -> dict[str, Any]:
    """Manda un turno con el LLM mockeado y devuelve el contexto que llegó al agente."""
    sub = AsyncMock()
    sub.process = AsyncMock(side_effect=lambda req, **_: _respuesta_de_consulta(req.request_id))
    agent_chat = _mock_agent_chat()
    with (
        patch(f"{_ORQ}.AgentCEO") as ceo,
        patch(f"{TEAM_EXECUTOR}.get_sub_agent", return_value=sub),
        patch(f"{_ORQ}.get_anthropic_async_client"),
        patch(f"{_ORQ}.AgentChat", return_value=agent_chat),
    ):
        ceo.return_value.process = AsyncMock(
            side_effect=lambda req: _make_ceo_plan_response(
                req.request_id, "consultar_ventas", "agent_analytics", "ANSWER_DATA_QUERY"
            )
        )
        resp = await ac.post(
            f"/api/v1/agent/{ruta}",
            json={"message": "mostrame las ventas", "conversation_id": conv_id},
            headers=_headers(user),
        )
    assert resp.status_code == 200, resp.text
    agent_chat.generate_response.assert_called_once()
    ctx: dict[str, Any] = agent_chat.generate_response.call_args.kwargs["conversation_ctx"]
    return ctx


async def _fila(session: AsyncSession, conv_id: str) -> AgentConversationContext:
    # Lo que haya quedado pendiente en la sesión baja a la base antes de releer:
    # un expire sin flush descartaría un cambio no volcado y el test pasaría vacío.
    await session.flush()
    session.expire_all()
    return (
        await session.execute(
            select(AgentConversationContext).where(
                AgentConversationContext.conversation_id == uuid.UUID(conv_id)
            )
        )
    ).scalar_one()


# ── El otro negocio: no lee ni pisa ──────────────────────────────────────────


@pytest.mark.parametrize("ruta", ["chat", "chat/stream"])
async def test_otro_negocio_con_el_id_ajeno_no_ve_los_turnos_ni_pisa_la_fila(
    ac: AsyncClient,
    session: AsyncSession,
    redis: FakeRedis,
    negocio_a: Tenant,
    negocio_b: Tenant,
    ruta: str,
) -> None:
    usuario_a = await _usuario(session, negocio_a)
    usuario_b = await _usuario(session, negocio_b)
    conv_id = await _conversacion_de_a(session, redis, negocio_a, usuario_a)
    tenant_a, tenant_b, user_a = negocio_a.tenant_id, negocio_b.tenant_id, usuario_a.user_id

    ctx = await _chat(ac, usuario_b, conv_id, ruta)

    assert _SECRETO not in json.dumps(ctx, ensure_ascii=False)
    assert ctx.get("summary") != "resumen de A"
    fila = await _fila(session, conv_id)
    assert fila.tenant_id == tenant_a
    assert fila.user_id == user_a
    assert [t["content"] for t in fila.turns] == ["¿cuánto gano?", _SECRETO]
    assert fila.summary == "resumen de A"
    # La clave de A en Redis tampoco se tocó; el turno de B quedó en la suya.
    en_a = json.loads(await redis.get(f"conv:{tenant_a}:{conv_id}") or "{}")
    assert len(en_a["turns"]) == 2
    en_b = json.loads(await redis.get(f"conv:{tenant_b}:{conv_id}") or "{}")
    assert [t["content"] for t in en_b["turns"]][0] == "mostrame las ventas"


async def test_la_conversacion_ajena_tampoco_se_lee_si_redis_no_la_tiene(
    ac: AsyncClient,
    session: AsyncSession,
    redis: FakeRedis,
    negocio_a: Tenant,
    negocio_b: Tenant,
) -> None:
    """Sin caché, el fallback a PostgreSQL es el que tiene que mirar el tenant."""
    usuario_a = await _usuario(session, negocio_a)
    usuario_b = await _usuario(session, negocio_b)
    conv_id = await _conversacion_de_a(session, redis, negocio_a, usuario_a)
    redis._store.clear()

    ctx = await _chat(ac, usuario_b, conv_id)

    assert _SECRETO not in json.dumps(ctx, ensure_ascii=False)
    assert await redis.get(f"conv:{negocio_a.tenant_id}:{conv_id}") is None
    fila = await _fila(session, conv_id)
    assert [t["content"] for t in fila.turns] == ["¿cuánto gano?", _SECRETO]


# ── El mismo negocio: sigue su conversación ──────────────────────────────────


async def test_el_mismo_negocio_continua_su_conversacion(
    ac: AsyncClient,
    session: AsyncSession,
    redis: FakeRedis,
    negocio_a: Tenant,
) -> None:
    usuario_a = await _usuario(session, negocio_a)
    conv_id = await _conversacion_de_a(session, redis, negocio_a, usuario_a)
    redis._store.clear()  # que el contexto salga de la base, el camino con chequeo

    ctx = await _chat(ac, usuario_a, conv_id)

    assert _SECRETO in json.dumps(ctx, ensure_ascii=False)
    fila = await _fila(session, conv_id)
    assert [t["content"] for t in fila.turns][:2] == ["¿cuánto gano?", _SECRETO]
    assert [t["content"] for t in fila.turns][2] == "mostrame las ventas"
    assert len(fila.turns) == 4


# ── Puntero a la acción pendiente y archivo pendiente ───────────────────────


async def test_el_si_de_otro_negocio_no_usa_ni_borra_el_puntero_ajeno(
    session: AsyncSession, redis: FakeRedis, negocio_a: Tenant, negocio_b: Tenant
) -> None:
    usuario_b = await _usuario(session, negocio_b)
    conv_id = str(uuid.uuid4())
    svc = ConversationService(redis, session)  # type: ignore[arg-type]
    accion = str(uuid.uuid4())
    await svc.record_pending_action(
        conv_id,
        tenant_id=negocio_a.tenant_id,
        pending_action_id=accion,
        approval_group_id=None,
        status="requires_approval",
    )

    resp = await _try_nl_confirmation(
        body=ChatRequest(message="sí", conversation_id=conv_id),
        current_user=usuario_b,
        db=session,
        redis=redis,  # type: ignore[arg-type]
    )

    assert resp is None
    puntero = await svc.get_pending_action(conv_id, tenant_id=negocio_a.tenant_id)
    assert puntero is not None
    assert puntero["pending_action_id"] == accion


async def test_el_archivo_pendiente_de_otro_negocio_no_pisa_el_propio(
    redis: FakeRedis, negocio_a: Tenant, negocio_b: Tenant
) -> None:
    with patch(f"{_ORQ}.get_anthropic_async_client"):
        orq = ChatOrchestrator()
    conv_id = str(uuid.uuid4())
    await orq._remember_pending_files(
        conv_id,
        [{"file_id": "archivo-de-a", "filename": "a.xlsx"}],
        redis,  # type: ignore[arg-type]
        negocio_a.tenant_id,
    )
    await orq._remember_pending_files(
        conv_id,
        [{"file_id": "archivo-de-b", "filename": "b.xlsx"}],
        redis,  # type: ignore[arg-type]
        negocio_b.tenant_id,
    )

    crudo = await redis.get(ChatOrchestrator._pending_file_key(conv_id, negocio_a.tenant_id))
    assert crudo is not None
    assert json.loads(crudo)["file_ids"] == ["archivo-de-a"]
