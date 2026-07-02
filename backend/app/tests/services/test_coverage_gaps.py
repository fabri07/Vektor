"""Tests de coverage-gap logging (Parte A — backlog de producto desde rechazos).

Cubre:
- CoverageGapService.log_gap: persiste, trunca, valida reason, y NUNCA lanza.
- Cableado en ChatOrchestrator: out_of_scope / intent_desconocido / baja_confianza
  se loguean con el reason correcto SIN cambiar la respuesta al usuario.
- Tolerancia a fallos: si el logging explota, el usuario recibe su mensaje igual.
"""

from __future__ import annotations

import uuid
from contextlib import ExitStack, asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.agents.shared.schemas import (
    AgentRequest,
    AgentResponse,
    Confidence,
    RiskLevel,
)
from app.application.services.chat_orchestrator import (
    _NO_AGENT_MESSAGES,
    ChatOrchestrator,
)
from app.application.services.coverage_gap_service import CoverageGapService
from app.persistence.models.coverage_gap import ChatCoverageGap

ORCHESTRATOR = "app.application.services.chat_orchestrator"
GAP_SERVICE = "app.application.services.coverage_gap_service"


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_request(message: str = "cuál es la capital de Francia") -> AgentRequest:
    return AgentRequest(
        user_id=str(uuid.uuid4()),
        business_id=str(uuid.uuid4()),
        message=message,
        attachments=[],
        conversation_id=None,
    )


def _ceo_response(
    request_id: str,
    intent: str,
    *,
    confidence_float: float | None = None,
    plan: dict[str, Any] | None = None,
) -> AgentResponse:
    result: dict[str, Any] = {"intent": intent, "target_agent": None}
    if confidence_float is not None:
        result["confidence_float"] = confidence_float
    if plan is not None:
        result["plan"] = plan
    return AgentResponse(
        request_id=request_id,
        agent_name="agent_ceo",
        status="success",
        risk_level=RiskLevel.LOW,
        requires_approval=False,
        confidence=Confidence.LOW,
        result=result,
    )


def _fake_session_factory(session: Any):
    """Factory async-context-manager que devuelve la sesión dada."""

    @asynccontextmanager
    async def _factory():
        yield session

    return _factory


@pytest.fixture
def mock_db():
    db = AsyncMock()
    tenant = MagicMock()
    tenant.display_name = "Kiosco El Rápido"
    db.get = AsyncMock(return_value=tenant)
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = MagicMock(vertical_code="kiosco_almacen")
    db.execute = AsyncMock(return_value=result_mock)
    return db


@pytest.fixture
def mock_redis():
    redis = AsyncMock()
    redis.get = AsyncMock(return_value=None)
    redis.setex = AsyncMock()
    return redis


# ── CoverageGapService ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_log_gap_persists_row(db_session: AsyncSession, sample_tenant) -> None:
    """El gap se persiste con todos los campos (round-trip real sobre el ORM)."""
    svc = CoverageGapService()
    with patch(
        f"{GAP_SERVICE}.async_session_factory", _fake_session_factory(db_session)
    ):
        await svc.log_gap(
            tenant_id=sample_tenant.tenant_id,
            user_id=None,
            original_message="quiero facturar electrónico con AFIP",
            fallback_reason="out_of_scope",
            classified_intent="out_of_scope",
            confidence=0.9,
            ui_context={"view": "dashboard"},
        )

    row = (
        await db_session.execute(select(ChatCoverageGap))
    ).scalar_one()
    assert row.tenant_id == sample_tenant.tenant_id
    assert row.original_message == "quiero facturar electrónico con AFIP"
    assert row.fallback_reason == "out_of_scope"
    assert row.classified_intent == "out_of_scope"
    assert row.confidence == 0.9
    assert row.ui_context == {"view": "dashboard"}
    assert row.reviewed is False


@pytest.mark.asyncio
async def test_log_gap_truncates_long_message(
    db_session: AsyncSession, sample_tenant
) -> None:
    svc = CoverageGapService()
    with patch(
        f"{GAP_SERVICE}.async_session_factory", _fake_session_factory(db_session)
    ):
        await svc.log_gap(
            tenant_id=sample_tenant.tenant_id,
            original_message="x" * 10_000,
            fallback_reason="intent_desconocido",
        )
    row = (await db_session.execute(select(ChatCoverageGap))).scalar_one()
    assert len(row.original_message) == 4000


@pytest.mark.asyncio
async def test_log_gap_unknown_reason_is_dropped(
    db_session: AsyncSession, sample_tenant
) -> None:
    """Reason fuera del set cerrado → no inserta (y no lanza)."""
    svc = CoverageGapService()
    with patch(
        f"{GAP_SERVICE}.async_session_factory", _fake_session_factory(db_session)
    ):
        await svc.log_gap(
            tenant_id=sample_tenant.tenant_id,
            original_message="lo que sea",
            fallback_reason="razon_inventada",
        )
    rows = (await db_session.execute(select(ChatCoverageGap))).scalars().all()
    assert rows == []


@pytest.mark.asyncio
async def test_log_gap_never_raises_on_db_error() -> None:
    """Fallo de commit → se traga: el contrato es best-effort."""
    broken_session = AsyncMock()
    broken_session.add = MagicMock()
    broken_session.commit = AsyncMock(side_effect=RuntimeError("db down"))
    svc = CoverageGapService()
    with patch(
        f"{GAP_SERVICE}.async_session_factory", _fake_session_factory(broken_session)
    ):
        # No debe propagar la excepción.
        await svc.log_gap(
            tenant_id=uuid.uuid4(),
            original_message="hola",
            fallback_reason="baja_confianza",
        )


# ── Cableado en ChatOrchestrator ─────────────────────────────────────────────


def _enter_orchestrator_patches(
    stack: ExitStack, ceo_intent: str, **ceo_kwargs: Any
) -> None:
    """Aplica los patches comunes: CEO mockeado + servicios de contexto silenciados."""
    stack.enter_context(
        patch(
            f"{ORCHESTRATOR}.AgentCEO",
            return_value=MagicMock(
                process=AsyncMock(
                    side_effect=lambda req: _ceo_response(
                        req.request_id, ceo_intent, **ceo_kwargs
                    )
                )
            ),
        )
    )
    stack.enter_context(patch(f"{ORCHESTRATOR}.get_anthropic_async_client"))
    stack.enter_context(patch(f"{ORCHESTRATOR}.AgentChat"))
    stack.enter_context(patch(f"{ORCHESTRATOR}.BusinessMemoryService"))
    stack.enter_context(patch(f"{ORCHESTRATOR}.AgentMemoryService"))


@pytest.mark.asyncio
async def test_out_of_scope_logs_gap_and_keeps_message(mock_db, mock_redis) -> None:
    request = _make_request("cuál es la capital de Francia")
    with ExitStack() as stack:
        _enter_orchestrator_patches(stack, "out_of_scope")
        gap_cls = stack.enter_context(patch(f"{GAP_SERVICE}.CoverageGapService"))
        gap_cls.return_value.log_gap = AsyncMock()
        response = await ChatOrchestrator().handle(
            request, mock_db, mock_redis, uuid.uuid4(), uuid.uuid4()
        )

    # La respuesta al usuario es EXACTAMENTE la misma de siempre.
    assert response.message == _NO_AGENT_MESSAGES["out_of_scope"]
    gap_cls.return_value.log_gap.assert_awaited_once()
    kwargs = gap_cls.return_value.log_gap.await_args.kwargs
    assert kwargs["fallback_reason"] == "out_of_scope"
    assert kwargs["original_message"] == "cuál es la capital de Francia"


@pytest.mark.asyncio
async def test_low_confidence_gate_logs_baja_confianza(mock_db, mock_redis) -> None:
    """CEO clasifica con confianza < 0.72 → requires_clarification + gap logueado."""
    plan = {
        "plan_id": str(uuid.uuid4()),
        "intent": "consulta_libre",
        "tasks": [
            {
                "task_id": str(uuid.uuid4()),
                "agent": "agent_income",
                "action_type": "ANSWER_DATA_QUERY",
                "entities": {},
                "depends_on": [],
                "approval_group": None,
            }
        ],
        "requires_synthesis": False,
        "fallback_message": None,
    }
    request = _make_request("mmm lo de la cosa esa de las ventas")
    with ExitStack() as stack:
        _enter_orchestrator_patches(
            stack, "consulta_libre", confidence_float=0.5, plan=plan
        )
        gap_cls = stack.enter_context(patch(f"{GAP_SERVICE}.CoverageGapService"))
        gap_cls.return_value.log_gap = AsyncMock()
        response = await ChatOrchestrator().handle(
            request, mock_db, mock_redis, uuid.uuid4(), uuid.uuid4()
        )

    assert response.status == "requires_clarification"
    assert response.question  # el usuario recibe la pregunta de siempre
    kwargs = gap_cls.return_value.log_gap.await_args.kwargs
    assert kwargs["fallback_reason"] == "baja_confianza"
    assert kwargs["confidence"] == 0.5
    assert kwargs["classified_intent"] == "consulta_libre"


@pytest.mark.asyncio
async def test_gap_logging_failure_does_not_break_response(mock_db, mock_redis) -> None:
    """Si el logging explota (constructor incluido), el usuario ve su mensaje igual."""
    request = _make_request("cuál es la capital de Francia")
    with ExitStack() as stack:
        _enter_orchestrator_patches(stack, "out_of_scope")
        stack.enter_context(
            patch(
                f"{GAP_SERVICE}.CoverageGapService",
                side_effect=RuntimeError("boom"),
            )
        )
        response = await ChatOrchestrator().handle(
            request, mock_db, mock_redis, uuid.uuid4(), uuid.uuid4()
        )

    assert response.message == _NO_AGENT_MESSAGES["out_of_scope"]
    assert response.status == "success"
