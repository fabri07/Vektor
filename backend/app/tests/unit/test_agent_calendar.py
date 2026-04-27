"""Unit tests — AgentCalendar sin gateway MCP real."""

from unittest.mock import patch

import pytest

from app.application.agents.calendar.agent import AgentCalendar
from app.application.agents.shared.schemas import ActionType, AgentRequest, RiskLevel


def _req(message: str) -> AgentRequest:
    return AgentRequest(user_id="u1", business_id="t1", message=message)


@pytest.mark.asyncio
async def test_query_returns_requires_approval():
    agent = AgentCalendar(gateway=object())  # type: ignore[arg-type]
    resp = await agent.process(_req("ver agenda de esta semana"))
    assert resp.status == "requires_approval"
    assert resp.risk_level == RiskLevel.LOW


@pytest.mark.asyncio
async def test_create_event_with_title_requires_approval():
    agent = AgentCalendar(gateway=object())  # type: ignore[arg-type]
    with patch.object(agent, "_extract_event_data", return_value={
        "has_enough_info": True,
        "summary": "Reunión con el contador",
        "start_datetime": "2026-04-28T10:00:00",
        "end_datetime": "2026-04-28T11:00:00",
        "attendees": [],
        "description": "",
    }):
        resp = await agent.process(_req("crear reunión con el contador el lunes"))
    assert resp.status == "requires_approval"
    assert resp.result["action_type"] == ActionType.CREATE_CALENDAR_EVENT
    assert resp.risk_level == RiskLevel.MEDIUM
    assert resp.requires_approval is True


@pytest.mark.asyncio
async def test_missing_event_data_returns_clarification():
    agent = AgentCalendar(gateway=object())  # type: ignore[arg-type]
    with patch.object(agent, "_extract_event_data", return_value={"has_enough_info": False}):
        resp = await agent.process(_req("necesito agendar algo para mañana"))
    assert resp.status == "requires_clarification"
    assert resp.question is not None


@pytest.mark.asyncio
async def test_requires_google_auth_without_gateway():
    agent = AgentCalendar(gateway=None)
    resp = await agent.process(_req("ver agenda de mañana"))
    assert resp.status == "requires_google_auth"
