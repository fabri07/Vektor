"""Unit tests — AgentSync sin gateway MCP real."""

import pytest

from app.application.agents.shared.schemas import ActionType, AgentRequest, RiskLevel
from app.application.agents.sync.agent import AgentSync


def _req(message: str) -> AgentRequest:
    return AgentRequest(user_id="u1", business_id="t1", message=message)


@pytest.mark.asyncio
async def test_export_sales_to_sheets():
    agent = AgentSync(gateway=object())  # type: ignore[arg-type]
    resp = await agent.process(_req("exportar ventas a Google Sheets"))
    assert resp.status == "requires_approval"
    assert resp.result["action_type"] == ActionType.SYNC_TO_GOOGLE
    assert resp.result["sync_type"] == "export_sales_to_sheets"
    assert resp.risk_level == RiskLevel.MEDIUM


@pytest.mark.asyncio
async def test_export_report_to_docs():
    agent = AgentSync(gateway=object())  # type: ignore[arg-type]
    resp = await agent.process(_req("exportar reporte a Google Docs"))
    assert resp.result["sync_type"] == "export_report_to_docs"
    assert resp.result["mcp_tool"] == "google.docs.create_document"


@pytest.mark.asyncio
async def test_import_from_sheets():
    agent = AgentSync(gateway=object())  # type: ignore[arg-type]
    resp = await agent.process(_req("importar desde sheets los productos"))
    assert resp.result["sync_type"] == "import_from_sheets"
    assert resp.result["mcp_tool"] == "google.sheets.read_range"


@pytest.mark.asyncio
async def test_unknown_sync_type_returns_clarification():
    agent = AgentSync(gateway=object())  # type: ignore[arg-type]
    resp = await agent.process(_req("sincronizar algo con google"))
    assert resp.status == "requires_clarification"
    assert resp.question is not None


@pytest.mark.asyncio
async def test_requires_google_auth_without_gateway():
    agent = AgentSync(gateway=None)
    resp = await agent.process(_req("exportar ventas a Google Sheets"))
    assert resp.status == "requires_google_auth"
