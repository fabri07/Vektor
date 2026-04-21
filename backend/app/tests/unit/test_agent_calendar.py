"""Unit tests — AgentCalendar sin gateway MCP real."""

import pytest

from app.application.agents.calendar.agent import AgentCalendar
from app.application.agents.shared.schemas import ActionType, AgentRequest, RiskLevel


def _req(message: str) -> AgentRequest:
    return AgentRequest(user_id="u1", business_id="t1", message=message)


@pytest.mark.asyncio
async def test_query_returns_success():
    agent = AgentCalendar()
    resp = await agent.process(_req("ver agenda de esta semana"))
    assert resp.status == "success"
    assert resp.risk_level == RiskLevel.LOW


@pytest.mark.asyncio
async def test_create_event_with_title_requires_approval():
    agent = AgentCalendar()
    resp = await agent.process(_req("crear reunión con el contador el lunes"))
    assert resp.status == "requires_approval"
    assert resp.result["action_type"] == ActionType.CREATE_CALENDAR_EVENT
    assert resp.risk_level == RiskLevel.MEDIUM
    assert resp.requires_approval is True


@pytest.mark.asyncio
async def test_missing_event_data_returns_clarification():
    # El mensaje no contiene keyword de query ("calendar" está en "calendario",
    # pero "agregar" no es una keyword de query), y tampoco contiene keywords
    # de evento (reunión, meeting, etc.) → _extract_event_data no extrae summary
    # → requires_clarification.
    # Usamos un mensaje sin la palabra "calendario" para evitar el match de substring.
    agent = AgentCalendar()
    resp = await agent.process(_req("necesito agendar algo para mañana"))
    assert resp.status == "requires_clarification"
    assert resp.question is not None


@pytest.mark.asyncio
async def test_mode_informational_without_gateway():
    agent = AgentCalendar(gateway=None)
    resp = await agent.process(_req("ver agenda de mañana"))
    assert resp.result.get("mode") == "informational"
