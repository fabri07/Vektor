"""Unit tests — AgentSupplier sin gateway MCP real."""

import pytest

from app.application.agents.shared.schemas import ActionType, AgentRequest, RiskLevel
from app.application.agents.supplier.agent import AgentSupplier


def _req(message: str) -> AgentRequest:
    return AgentRequest(user_id="u1", business_id="t1", message=message)


@pytest.mark.asyncio
async def test_draft_intent_returns_create_draft():
    agent = AgentSupplier()
    resp = await agent.process(_req("redactar email al proveedor de verduras"))
    assert resp.result["action_type"] == ActionType.CREATE_SUPPLIER_DRAFT
    assert resp.status == "requires_approval"
    assert resp.risk_level == RiskLevel.LOW


@pytest.mark.asyncio
async def test_inbox_intent_returns_classify():
    agent = AgentSupplier()
    resp = await agent.process(_req("revisar gmail de proveedores"))
    assert resp.result["action_type"] == ActionType.CLASSIFY_GMAIL_MESSAGE
    assert resp.status == "requires_approval"


@pytest.mark.asyncio
async def test_purchase_intent_returns_register():
    agent = AgentSupplier()
    resp = await agent.process(_req("registrar compra al proveedor de $15000"))
    assert resp.result["action_type"] == ActionType.REGISTER_PURCHASE
    assert resp.risk_level == RiskLevel.MEDIUM
    assert resp.requires_approval is True


@pytest.mark.asyncio
async def test_unknown_returns_clarification():
    agent = AgentSupplier()
    resp = await agent.process(_req("proveedor"))
    assert resp.status == "requires_clarification"
    assert resp.question is not None


@pytest.mark.asyncio
async def test_mode_informational_without_gateway():
    agent = AgentSupplier(gateway=None)
    resp = await agent.process(_req("borrador para proveedor"))
    assert resp.result.get("mode") == "informational"
