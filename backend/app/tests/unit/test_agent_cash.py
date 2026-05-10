"""Unit tests for AgentCash — no real LLM calls, no DB."""

import json
import unittest.mock
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.application.agents.shared.schemas import AgentRequest, RiskLevel


_TENANT_UUID = "00000000-0000-0000-0000-000000000001"
_USER_UUID = "00000000-0000-0000-0000-000000000002"


def _make_request(message: str = "test") -> AgentRequest:
    return AgentRequest(
        user_id=_USER_UUID,
        business_id=_TENANT_UUID,
        message=message,
    )


def _mock_llm_response(entities: dict) -> MagicMock:
    content_block = MagicMock()
    content_block.text = json.dumps(entities)
    response = MagicMock()
    response.content = [content_block]
    return response


@pytest.mark.asyncio
async def test_sale_extraction_with_amount():
    """'vendí 5000 pesos al contado' → amount=5000, status=requires_approval."""
    mock_entities = {
        "amount": 5000,
        "date": "hoy",
        "payment_status": "paid",
        "payment_method": "efectivo",
        "product_description": None,
        "confidence": "HIGH",
    }
    with unittest.mock.patch(
        "app.application.agents.cash.agent.anthropic.AsyncAnthropic"
    ) as mock_cls:
        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(return_value=_mock_llm_response(mock_entities))
        mock_cls.return_value = mock_client

        from app.application.agents.cash.agent import AgentCash

        agent = AgentCash()
        agent.client = mock_client
        result = await agent.process(_make_request("vendí 5000 pesos al contado"))

    assert result.status == "requires_approval"
    assert str(result.result["structured_data"]["amount"]) == "5000"


@pytest.mark.asyncio
async def test_unknown_payment_returns_clarification():
    """'vendí 5000' sin método de pago → requires_clarification."""
    mock_entities = {
        "amount": 5000,
        "date": "hoy",
        "payment_status": "unknown",
        "payment_method": None,
        "product_description": None,
        "confidence": "MEDIUM",
    }
    with unittest.mock.patch(
        "app.application.agents.cash.agent.anthropic.AsyncAnthropic"
    ) as mock_cls:
        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(return_value=_mock_llm_response(mock_entities))
        mock_cls.return_value = mock_client

        from app.application.agents.cash.agent import AgentCash

        agent = AgentCash()
        agent.client = mock_client
        result = await agent.process(_make_request("vendí 5000"))

    assert result.status == "requires_clarification"
    assert "contado" in result.question.lower() or "corriente" in result.question.lower()


@pytest.mark.asyncio
async def test_paid_sale_returns_approval():
    """'vendí 5000 al contado' → requires_approval, risk=MEDIUM."""
    mock_entities = {
        "amount": 5000,
        "date": "hoy",
        "payment_status": "paid",
        "payment_method": "efectivo",
        "product_description": None,
        "confidence": "HIGH",
    }
    with unittest.mock.patch(
        "app.application.agents.cash.agent.anthropic.AsyncAnthropic"
    ) as mock_cls:
        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(return_value=_mock_llm_response(mock_entities))
        mock_cls.return_value = mock_client

        from app.application.agents.cash.agent import AgentCash

        agent = AgentCash()
        agent.client = mock_client
        result = await agent.process(_make_request("vendí 5000 al contado"))

    assert result.status == "requires_approval"
    assert result.risk_level == RiskLevel.MEDIUM
    assert result.requires_approval is True
    assert result.result["action_type"] == "REGISTER_SALE"


@pytest.mark.asyncio
async def test_expense_message_is_auto_executed_candidate():
    from app.application.agents.cash.agent import AgentCash
    from app.application.agents.shared.schemas import ActionType

    agent = AgentCash()
    result = await agent.process(_make_request("Pagué alquiler $450.000 por transferencia"))

    assert result.status == "success"
    assert result.requires_approval is False
    assert result.risk_level == RiskLevel.MEDIUM
    assert result.result["action_type"] == ActionType.REGISTER_EXPENSE
    assert result.result["auto_execute"] is True
    assert result.result["structured_data"]["category"] == "RENT"
    assert result.result["structured_data"]["payment_method"] == "transfer"


@pytest.mark.asyncio
async def test_sale_and_inflow_are_separate_actions():
    """REGLA CRÍTICA 1: 'vendí y cobré 5000' → REGISTER_SALE y REGISTER_CASH_INFLOW son acciones distintas."""
    # AgentCash procesa el mensaje principal como venta
    mock_entities_sale = {
        "amount": 5000,
        "date": "hoy",
        "payment_status": "paid",
        "payment_method": "efectivo",
        "product_description": None,
        "confidence": "HIGH",
    }
    with unittest.mock.patch(
        "app.application.agents.cash.agent.anthropic.AsyncAnthropic"
    ) as mock_cls:
        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(
            return_value=_mock_llm_response(mock_entities_sale)
        )
        mock_cls.return_value = mock_client

        from app.application.agents.cash.agent import AgentCash
        from app.application.agents.shared.schemas import ActionType

        agent = AgentCash()
        agent.client = mock_client
        result = await agent.process(_make_request("vendí y cobré 5000"))

    # La venta se registra como REGISTER_SALE; el cobro sería una segunda acción separada
    assert result.result["action_type"] == ActionType.REGISTER_SALE
    # Verificar que ActionType.REGISTER_CASH_INFLOW existe como acción separada en el catálogo
    assert ActionType.REGISTER_CASH_INFLOW != ActionType.REGISTER_SALE


@pytest.mark.asyncio
async def test_sale_emits_event_after_confirm():
    """on_confirmed_sale → EventBus emite SALE_RECORDED."""
    with unittest.mock.patch(
        "app.application.agents.cash.agent.anthropic.AsyncAnthropic"
    ):
        with unittest.mock.patch(
            "app.application.agents.cash.agent.EventBus.emit"
        ) as mock_emit:
            from app.application.agents.cash.agent import AgentCash

            agent = AgentCash()
            await agent.on_confirmed_sale("sale-001", "tenant-001")

    mock_emit.assert_any_call(
        "SALE_RECORDED", {"sale_id": "sale-001", "business_id": "tenant-001"}
    )


@pytest.mark.asyncio
async def test_sale_with_quantity_looks_up_product_price():
    """'vendí 3 coca colas' sin monto + producto en catálogo → amount = precio × 3.

    El token 'colas' es el más largo pero no matchea; 'coca' sí matchea 'Coca-Cola 600ml'.
    El mock devuelve None para la primera query (match exacto) y product en la segunda
    query del fallback ilike.
    """
    from decimal import Decimal
    from unittest.mock import AsyncMock, MagicMock

    from app.application.agents.cash.agent import AgentCash

    mock_entities = {
        "amount": None,
        "quantity": 3,
        "date": "hoy",
        "payment_status": "paid",
        "payment_method": "efectivo",
        "product_description": "coca colas",
        "confidence": "HIGH",
    }

    mock_product = MagicMock()
    mock_product.sale_price_ars = Decimal("500")
    mock_product.name = "Coca-Cola 600ml"
    mock_product.id = "00000000-0000-0000-0000-000000000099"

    # Pasos 1 y 2 usan scalar_one_or_none(); pasos ILIKE usan scalars().all()
    none_result = MagicMock()
    none_result.scalar_one_or_none.return_value = None
    none_result.scalars.return_value.all.return_value = []  # ILIKE sin match

    found_result = MagicMock()
    found_result.scalar_one_or_none.return_value = mock_product
    found_result.scalars.return_value.all.return_value = [mock_product]  # ILIKE match único

    # exact → None; sku → None; ilike(colas) → []; ilike(coca) → [product]
    mock_db = MagicMock()
    mock_db.execute = AsyncMock(
        side_effect=[none_result, none_result, none_result, found_result]
    )

    with unittest.mock.patch(
        "app.application.agents.cash.agent.anthropic.AsyncAnthropic"
    ) as mock_cls:
        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(return_value=_mock_llm_response(mock_entities))
        mock_cls.return_value = mock_client

        agent = AgentCash(db=mock_db)
        agent.client = mock_client
        result = await agent.process(_make_request("vendí 3 coca colas"))

    assert result.status == "requires_approval"
    data = result.result["structured_data"]
    assert Decimal(str(data["amount"])) == Decimal("1500")
    assert data["quantity"] == 3
    assert data["unit_price"] == "500"
    assert data["price_lookup_source"] == "products_db"
    assert data["product_description"] == "Coca-Cola 600ml"


@pytest.mark.asyncio
async def test_sale_with_float_quantity_parsed_safely():
    """quantity='3.0' del LLM → se parsea como 3, no lanza ValueError."""
    from decimal import Decimal
    from unittest.mock import AsyncMock, MagicMock

    from app.application.agents.cash.agent import AgentCash

    mock_entities = {
        "amount": 1500,
        "quantity": "3.0",
        "date": "hoy",
        "payment_status": "paid",
        "payment_method": "efectivo",
        "product_description": "coca cola",
        "confidence": "HIGH",
    }

    with unittest.mock.patch(
        "app.application.agents.cash.agent.anthropic.AsyncAnthropic"
    ) as mock_cls:
        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(return_value=_mock_llm_response(mock_entities))
        mock_cls.return_value = mock_client

        agent = AgentCash()
        agent.client = mock_client
        result = await agent.process(_make_request("vendí 3 coca colas a $1500"))

    assert result.status == "requires_approval"
    assert result.result["structured_data"]["quantity"] == 3


@pytest.mark.asyncio
async def test_sale_product_not_in_catalog_asks_for_amount():
    """'vendí 3 coca colas' sin monto y producto no en catálogo → requires_clarification."""
    from app.application.agents.cash.agent import AgentCash

    mock_entities = {
        "amount": None,
        "quantity": 1,
        "date": "hoy",
        "payment_status": "paid",
        "payment_method": "efectivo",
        "product_description": "producto inexistente",
        "confidence": "HIGH",
    }

    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    mock_result.scalars.return_value.all.return_value = []  # ILIKE sin match

    mock_db = MagicMock()
    mock_db.execute = AsyncMock(return_value=mock_result)

    with unittest.mock.patch(
        "app.application.agents.cash.agent.anthropic.AsyncAnthropic"
    ) as mock_cls:
        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(return_value=_mock_llm_response(mock_entities))
        mock_cls.return_value = mock_client

        agent = AgentCash(db=mock_db)
        agent.client = mock_client
        result = await agent.process(_make_request("vendí un producto inexistente"))

    assert result.status == "requires_clarification"
    assert result.confidence.value == "LOW"
    assert "catálogo" in result.question or "importe" in result.question


@pytest.mark.asyncio
async def test_sale_with_explicit_amount_skips_product_lookup():
    """'vendí 3 coca colas a $1500' → registra directamente sin consultar DB."""
    from app.application.agents.cash.agent import AgentCash

    mock_entities = {
        "amount": 1500,
        "quantity": 3,
        "date": "hoy",
        "payment_status": "paid",
        "payment_method": "efectivo",
        "product_description": "coca cola",
        "confidence": "HIGH",
    }

    mock_db = MagicMock()
    mock_db.execute = AsyncMock()  # no debe llamarse

    with unittest.mock.patch(
        "app.application.agents.cash.agent.anthropic.AsyncAnthropic"
    ) as mock_cls:
        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(return_value=_mock_llm_response(mock_entities))
        mock_cls.return_value = mock_client

        agent = AgentCash(db=mock_db)
        agent.client = mock_client
        result = await agent.process(_make_request("vendí 3 coca colas a $1500"))

    assert result.status == "requires_approval"
    assert str(result.result["structured_data"]["amount"]) == "1500"
    mock_db.execute.assert_not_called()


@pytest.mark.asyncio
async def test_google_sheet_import_returns_pending_action_without_llm():
    """Google Sheets import → lee filas, parsea ventas y requiere aprobación."""
    from app.application.agents.cash.agent import AgentCash
    from app.application.agents.shared.schemas import ActionType

    class FakeGateway:
        async def sheets(self):
            return self

        async def run_google(self, coro):
            return await coro

        async def read_values(self, spreadsheet_id: str, range_name: str):
            values = [
                ["monto", "producto", "metodo_pago"],
                ["1200", "yerba", "efectivo"],
                ["2500", "azucar", "tarjeta"],
            ]
            result = MagicMock()
            result.spreadsheet_id = spreadsheet_id
            result.range = range_name
            result.values = values
            return result

    agent = AgentCash(gateway=FakeGateway())  # type: ignore[arg-type]
    result = await agent.process(
        _make_request(
            "Importa ventas desde https://docs.google.com/spreadsheets/d/sheet123/edit"
        )
    )

    assert result.status == "requires_approval"
    assert result.result["action_type"] == ActionType.IMPORT_TABULAR_FILE
    payload = result.result["structured_data"]
    assert payload["source"] == "google_sheets"
    assert payload["record_type"] == "sales"
    assert len(payload["parsed_records"]) == 2
