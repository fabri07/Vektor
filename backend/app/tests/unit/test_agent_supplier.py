"""Unit tests — AgentSupplier sin gateway MCP real."""

from unittest.mock import patch

from app.application.agents.shared.schemas import ActionType, AgentRequest, RiskLevel
from app.application.agents.supplier.agent import AgentSupplier


def _req(message: str) -> AgentRequest:
    return AgentRequest(user_id="u1", business_id="t1", message=message)


async def test_draft_intent_returns_create_draft():
    agent = AgentSupplier()
    with patch.object(
        agent,
        "_generate_email_draft",
        return_value=(
            {
                "has_enough_info": True,
                "to_name": "proveedor de verduras",
                "to_email": None,
                "subject": "Consulta de precios",
                "body": "Estimado proveedor, necesito cotización actualizada de verduras.",
            },
            None,
        ),
    ):
        resp = await agent.process(_req("redactar email al proveedor de verduras"))
    assert resp.result["action_type"] == ActionType.CREATE_SUPPLIER_DRAFT
    assert resp.status == "requires_approval"
    assert resp.risk_level == RiskLevel.MEDIUM


async def test_inbox_intent_returns_classify():
    agent = AgentSupplier()
    resp = await agent.process(_req("revisar gmail de proveedores"))
    assert resp.result["action_type"] == ActionType.CLASSIFY_GMAIL_MESSAGE
    assert resp.status == "requires_approval"


async def test_purchase_intent_returns_register():
    agent = AgentSupplier()
    with patch.object(
        agent,
        "_extract_purchase_entities",
        return_value=(
            {
                "amount": "15000",
                "supplier_name": "proveedor",
                "product_name": None,
                "sku": None,
                "qty": None,
                "unit_cost": None,
                "transaction_date": "2026-04-27",
            },
            None,
        ),
    ):
        resp = await agent.process(_req("registrar compra al proveedor de $15000"))
    assert resp.result["action_type"] == ActionType.REGISTER_PURCHASE
    assert resp.risk_level == RiskLevel.MEDIUM
    assert resp.requires_approval is True


async def test_ambiguous_message_routes_to_query():
    # Mensaje ambiguo → cae en _handle_query (sin session → SIN_DATOS)
    agent = AgentSupplier()
    resp = await agent.process(_req("proveedor"))
    assert resp.status == "success"
    assert resp.result.get("summary") == "SIN_DATOS"


async def test_mode_informational_without_gateway():
    agent = AgentSupplier(gateway=None)
    with patch.object(
        agent,
        "_generate_email_draft",
        return_value=(
            {
                "has_enough_info": True,
                "to_name": "proveedor",
                "to_email": None,
                "subject": "Borrador",
                "body": "Mensaje de prueba.",
            },
            None,
        ),
    ):
        resp = await agent.process(_req("borrador para proveedor"))
    assert resp.result.get("mode") == "informational"
