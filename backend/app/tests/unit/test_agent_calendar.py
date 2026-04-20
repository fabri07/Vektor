"""Tests unitarios para AgentCalendar — sin llamadas reales al LLM ni a Google."""

from __future__ import annotations

import json
import unittest.mock
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.application.agents.shared.schemas import AgentRequest, Confidence, RiskLevel


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_request(message: str = "agendá una reunión") -> AgentRequest:
    return AgentRequest(
        user_id="user-123",
        business_id="tenant-456",
        message=message,
    )


def _mock_llm_response(intent: str, entities: dict | None = None) -> MagicMock:
    content_block = MagicMock()
    content_block.text = json.dumps({"intent": intent, "entities": entities or {}})
    response = MagicMock()
    response.content = [content_block]
    return response


def _make_gateway(*, connected: bool = True, events: list | None = None) -> MagicMock:
    """Construye un mock de GoogleWorkspaceGateway."""
    gateway = MagicMock()
    gateway.is_connected = AsyncMock(return_value=connected)
    gateway.get_oauth_url = AsyncMock(return_value="https://accounts.google.com/oauth?scope=calendar")
    gateway.calendar_get_agenda = AsyncMock(return_value=events or [])
    return gateway


# ── Tests ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_google_not_connected_returns_auth_url():
    """Si gateway.is_connected() es False → status requires_google_auth con auth_url."""
    from app.application.agents.calendar.agent import AgentCalendar

    gateway = _make_gateway(connected=False)
    agent = AgentCalendar(gateway=gateway)

    result = await agent.process(_make_request("ver mi agenda"))

    assert result.status == "requires_google_auth"
    assert result.risk_level == RiskLevel.LOW
    assert result.requires_approval is False
    assert "auth_url" in result.result
    assert result.result["auth_url"] == "https://accounts.google.com/oauth?scope=calendar"
    assert "calendar" in result.result["scopes_requested"][0]


@pytest.mark.asyncio
async def test_calendar_review_returns_events_list():
    """review_calendar con eventos → status success, lista de eventos en result."""
    from app.application.agents.calendar.agent import AgentCalendar

    sample_events = [
        {"title": "Reunión proveedor", "start": "2026-04-21T10:00:00+00:00", "end": "2026-04-21T11:00:00+00:00", "description": ""},
        {"title": "Cierre caja", "start": "2026-04-22T18:00:00+00:00", "end": "2026-04-22T18:30:00+00:00", "description": ""},
    ]
    gateway = _make_gateway(connected=True, events=sample_events)

    agent = AgentCalendar(gateway=gateway)
    # Mock del cliente LLM para que clasifique review_calendar
    mock_llm = MagicMock()
    mock_llm.messages.create = AsyncMock(
        return_value=_mock_llm_response("review_calendar")
    )

    with unittest.mock.patch.object(agent, "_client", mock_llm):
        result = await agent.process(_make_request("qué tengo esta semana"))

    assert result.status == "success"
    assert result.risk_level == RiskLevel.LOW
    assert result.requires_approval is False
    assert result.confidence == Confidence.HIGH
    assert "events" in result.result
    assert len(result.result["events"]) == 2
    assert "Reunión proveedor" in result.result["summary"]


@pytest.mark.asyncio
async def test_calendar_review_no_events():
    """review_calendar sin eventos → summary indica que no hay eventos."""
    from app.application.agents.calendar.agent import AgentCalendar

    gateway = _make_gateway(connected=True, events=[])
    agent = AgentCalendar(gateway=gateway)
    mock_llm = MagicMock()
    mock_llm.messages.create = AsyncMock(
        return_value=_mock_llm_response("review_calendar")
    )

    with unittest.mock.patch.object(agent, "_client", mock_llm):
        result = await agent.process(_make_request("qué tengo esta semana"))

    assert result.status == "success"
    assert result.result["events"] == []
    assert "No tenés eventos" in result.result["summary"]


@pytest.mark.asyncio
async def test_calendar_create_event_requires_approval():
    """schedule_event → requires_approval=True, risk MEDIUM, datos estructurados."""
    from app.application.agents.calendar.agent import AgentCalendar

    gateway = _make_gateway(connected=True)
    agent = AgentCalendar(gateway=gateway)
    mock_llm = MagicMock()
    mock_llm.messages.create = AsyncMock(
        return_value=_mock_llm_response(
            "schedule_event",
            {"title": "Reunión con contador", "date": "2026-05-01", "time": "15:00", "duration_minutes": 60},
        )
    )

    with unittest.mock.patch.object(agent, "_client", mock_llm):
        result = await agent.process(_make_request("agendá reunión con contador el 1 de mayo a las 15"))

    assert result.status == "requires_approval"
    assert result.risk_level == RiskLevel.MEDIUM
    assert result.requires_approval is True
    assert result.confidence == Confidence.HIGH  # title fue extraído
    assert result.result["structured_data"]["title"] == "Reunión con contador"
    assert result.result["structured_data"]["event_type"] == "calendar_create"
    assert "2026-05-01" in result.result["structured_data"]["start_datetime"]
    assert result.result["structured_data"]["duration_minutes"] == 60


@pytest.mark.asyncio
async def test_calendar_create_event_no_title_medium_confidence():
    """schedule_event sin título extraído → confidence MEDIUM."""
    from app.application.agents.calendar.agent import AgentCalendar

    gateway = _make_gateway(connected=True)
    agent = AgentCalendar(gateway=gateway)
    mock_llm = MagicMock()
    mock_llm.messages.create = AsyncMock(
        return_value=_mock_llm_response(
            "schedule_event",
            {"title": None, "date": "2026-05-02", "time": "10:00", "duration_minutes": 30},
        )
    )

    with unittest.mock.patch.object(agent, "_client", mock_llm):
        result = await agent.process(_make_request("agendá algo para mañana"))

    assert result.status == "requires_approval"
    assert result.confidence == Confidence.MEDIUM  # title es None


@pytest.mark.asyncio
async def test_calendar_create_event_no_date_defaults_tomorrow():
    """schedule_event sin fecha → fallback a mañana."""
    from app.application.agents.calendar.agent import AgentCalendar

    from datetime import datetime, timezone, timedelta

    gateway = _make_gateway(connected=True)
    agent = AgentCalendar(gateway=gateway)
    mock_llm = MagicMock()
    mock_llm.messages.create = AsyncMock(
        return_value=_mock_llm_response(
            "schedule_event",
            {"title": "Reunión", "date": None, "time": None, "duration_minutes": 60},
        )
    )

    with unittest.mock.patch.object(agent, "_client", mock_llm):
        result = await agent.process(_make_request("agendá una reunión"))

    sd = result.result["structured_data"]
    start = datetime.fromisoformat(sd["start_datetime"])
    tomorrow_start = (
        datetime.now(timezone.utc).replace(hour=9, minute=0, second=0, microsecond=0)
        + timedelta(days=1)
    )
    # Fecha debe ser mañana (mismo día)
    assert start.date() == tomorrow_start.date()


@pytest.mark.asyncio
async def test_calendar_no_gateway_returns_auth_url():
    """Sin gateway (None) → status requires_google_auth, auth_url vacío."""
    from app.application.agents.calendar.agent import AgentCalendar

    agent = AgentCalendar(gateway=None)
    result = await agent.process(_make_request("ver agenda"))

    assert result.status == "requires_google_auth"
    assert result.result["auth_url"] == ""


@pytest.mark.asyncio
async def test_ceo_routes_schedule_event_to_calendar():
    """CEO mapea intent schedule_event → agent_calendar."""
    import app.application.agents.ceo.agent as ceo_module

    assert "schedule_event" in ceo_module.INTENT_CATALOG
    assert ceo_module.INTENT_TO_AGENT["schedule_event"] == "agent_calendar"
    assert ceo_module.INTENT_TO_AGENT["review_calendar"] == "agent_calendar"
    assert ceo_module.INTENT_TO_AGENT["sync_to_google"] == "agent_calendar"


@pytest.mark.asyncio
async def test_ceo_routes_schedule_event_via_process():
    """CEO clasifica 'agendá' como schedule_event y enruta a agent_calendar."""
    from app.application.agents.ceo.agent import AgentCEO

    mock_client = MagicMock()
    mock_client.messages.create = AsyncMock(
        return_value=_mock_llm_response("schedule_event", {"title": "demo"})
    )

    agent = AgentCEO()
    agent.client = mock_client

    result = await agent.process(_make_request("agendá una reunión con el proveedor"))

    assert result.result["intent"] == "schedule_event"
    assert result.result["target_agent"] == "agent_calendar"
    assert result.result["action_type"] == "SYNC_TO_GOOGLE"


@pytest.mark.asyncio
async def test_calendar_get_agenda_error_returns_empty_list():
    """Si calendar_get_agenda lanza excepción, el agente retorna éxito con lista vacía."""
    from app.application.agents.calendar.agent import AgentCalendar

    gateway = _make_gateway(connected=True)
    gateway.calendar_get_agenda = AsyncMock(side_effect=RuntimeError("Google API timeout"))

    agent = AgentCalendar(gateway=gateway)
    mock_llm = MagicMock()
    mock_llm.messages.create = AsyncMock(
        return_value=_mock_llm_response("review_calendar")
    )

    with unittest.mock.patch.object(agent, "_client", mock_llm):
        result = await agent.process(_make_request("ver mi agenda"))

    assert result.status == "success"
    assert result.result["events"] == []
