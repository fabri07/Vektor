"""Tests para AgentCash (shim Stage 2b), AgentIncome y AgentExpense.

Las tests de lógica de ingresos apuntan a AgentIncome directamente.
Las tests de lógica de egresos apuntan a AgentExpense directamente.
Las tests del shim verifican el dispatch y la preservación de agent_name legacy.
"""

import json
import unittest.mock
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.application.agents.shared.schemas import ActionType, AgentRequest, RiskLevel
from app.domain.verticals import Vertical

_TENANT_UUID = "00000000-0000-0000-0000-000000000001"
_USER_UUID = "00000000-0000-0000-0000-000000000002"


def _make_request(message: str = "test") -> AgentRequest:
    return AgentRequest(
        user_id=_USER_UUID,
        business_id=_TENANT_UUID,
        message=message,
    )


def _mock_llm_response(entities: dict[str, Any]) -> MagicMock:
    content_block = MagicMock()
    content_block.text = json.dumps(entities)
    response = MagicMock()
    response.content = [content_block]
    response.usage = MagicMock(input_tokens=50, output_tokens=20)
    return response


def _vertical_result() -> MagicMock:
    """Resultado del SELECT de `business_profiles.vertical_code`."""
    result = MagicMock()
    result.scalar_one_or_none.return_value = Vertical.KIOSCO_ALMACEN.value
    return result


def _db_with_vertical(*results: MagicMock, default: MagicMock | None = None) -> MagicMock:
    """Sesión mockeada cuyo PRIMER execute resuelve el vertical del tenant.

    El agente ya no asume kiosco: lo lee del BusinessProfile antes de extraer la
    venta, así que la secuencia de queries arranca por ahí.
    """
    pending = iter([_vertical_result(), *results])

    def _next(*_args: Any, **_kwargs: Any) -> MagicMock:
        try:
            return next(pending)
        except StopIteration:
            if default is None:
                raise
            return default

    db = MagicMock()
    db.execute = AsyncMock(side_effect=_next)
    return db


# ── Tests de AgentIncome (lógica de ingresos) ─────────────────────────────────


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
        "app.application.agents.income.agent.anthropic.AsyncAnthropic"
    ) as mock_cls:
        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(return_value=_mock_llm_response(mock_entities))
        mock_cls.return_value = mock_client

        from app.application.agents.income.agent import AgentIncome

        agent = AgentIncome(default_vertical=Vertical.KIOSCO_ALMACEN)
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
        "app.application.agents.income.agent.anthropic.AsyncAnthropic"
    ) as mock_cls:
        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(return_value=_mock_llm_response(mock_entities))
        mock_cls.return_value = mock_client

        from app.application.agents.income.agent import AgentIncome

        agent = AgentIncome(default_vertical=Vertical.KIOSCO_ALMACEN)
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
        "app.application.agents.income.agent.anthropic.AsyncAnthropic"
    ) as mock_cls:
        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(return_value=_mock_llm_response(mock_entities))
        mock_cls.return_value = mock_client

        from app.application.agents.income.agent import AgentIncome

        agent = AgentIncome(default_vertical=Vertical.KIOSCO_ALMACEN)
        result = await agent.process(_make_request("vendí 5000 al contado"))

    assert result.status == "requires_approval"
    assert result.risk_level == RiskLevel.MEDIUM
    assert result.requires_approval is True
    assert result.result["action_type"] == "REGISTER_SALE"


@pytest.mark.asyncio
async def test_sale_and_inflow_are_separate_actions():
    """REGISTER_SALE y REGISTER_CASH_INFLOW son ActionTypes distintos."""
    mock_entities = {
        "amount": 5000,
        "date": "hoy",
        "payment_status": "paid",
        "payment_method": "efectivo",
        "product_description": None,
        "confidence": "HIGH",
    }
    with unittest.mock.patch(
        "app.application.agents.income.agent.anthropic.AsyncAnthropic"
    ) as mock_cls:
        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(return_value=_mock_llm_response(mock_entities))
        mock_cls.return_value = mock_client

        from app.application.agents.income.agent import AgentIncome

        agent = AgentIncome(default_vertical=Vertical.KIOSCO_ALMACEN)
        result = await agent.process(_make_request("vendí y cobré 5000"))

    assert result.result["action_type"] == ActionType.REGISTER_SALE
    assert ActionType.REGISTER_CASH_INFLOW != ActionType.REGISTER_SALE


@pytest.mark.asyncio
async def test_sale_with_quantity_looks_up_product_price():
    """'vendí 3 coca colas' sin monto + producto en catálogo → amount = precio × 3."""
    from app.application.agents.income.agent import AgentIncome

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

    none_result = MagicMock()
    none_result.scalar_one_or_none.return_value = None
    none_result.scalars.return_value.all.return_value = []

    found_result = MagicMock()
    found_result.scalar_one_or_none.return_value = mock_product
    found_result.scalars.return_value.all.return_value = [mock_product]

    mock_db = _db_with_vertical(none_result, none_result, none_result, found_result)

    with unittest.mock.patch(
        "app.application.agents.income.agent.anthropic.AsyncAnthropic"
    ) as mock_cls:
        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(return_value=_mock_llm_response(mock_entities))
        mock_cls.return_value = mock_client

        agent = AgentIncome(db=mock_db)
        result = await agent.process(_make_request("vendí 3 coca colas"))

    assert result.status == "requires_approval"
    data = result.result["structured_data"]
    assert Decimal(str(data["amount"])) == Decimal("1500")
    assert data["quantity"] == 3
    assert data["price_lookup_source"] == "products_db"


@pytest.mark.asyncio
async def test_sale_with_float_quantity_parsed_safely():
    """quantity='3.0' del LLM → se parsea como 3, no lanza ValueError."""
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
        "app.application.agents.income.agent.anthropic.AsyncAnthropic"
    ) as mock_cls:
        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(return_value=_mock_llm_response(mock_entities))
        mock_cls.return_value = mock_client

        from app.application.agents.income.agent import AgentIncome

        agent = AgentIncome(default_vertical=Vertical.KIOSCO_ALMACEN)
        result = await agent.process(_make_request("vendí 3 coca colas a $1500"))

    assert result.status == "requires_approval"
    assert result.result["structured_data"]["quantity"] == 3


@pytest.mark.asyncio
async def test_sale_product_not_in_catalog_asks_for_amount():
    """'vendí 3 coca colas' sin monto y producto no en catálogo → requires_clarification."""
    from app.application.agents.income.agent import AgentIncome

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
    mock_result.scalars.return_value.all.return_value = []

    mock_db = _db_with_vertical(default=mock_result)

    with unittest.mock.patch(
        "app.application.agents.income.agent.anthropic.AsyncAnthropic"
    ) as mock_cls:
        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(return_value=_mock_llm_response(mock_entities))
        mock_cls.return_value = mock_client

        agent = AgentIncome(db=mock_db)
        result = await agent.process(_make_request("vendí un producto inexistente"))

    assert result.status == "requires_clarification"
    assert "catálogo" in result.question or "importe" in result.question


@pytest.mark.asyncio
async def test_sale_with_explicit_amount_skips_product_lookup():
    """'vendí 3 coca colas a $1500' → registra directamente sin consultar DB."""
    from app.application.agents.income.agent import AgentIncome

    mock_entities = {
        "amount": 1500,
        "quantity": 3,
        "date": "hoy",
        "payment_status": "paid",
        "payment_method": "efectivo",
        "product_description": "coca cola",
        "confidence": "HIGH",
    }
    mock_db = _db_with_vertical()

    with unittest.mock.patch(
        "app.application.agents.income.agent.anthropic.AsyncAnthropic"
    ) as mock_cls:
        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(return_value=_mock_llm_response(mock_entities))
        mock_cls.return_value = mock_client

        agent = AgentIncome(db=mock_db)
        result = await agent.process(_make_request("vendí 3 coca colas a $1500"))

    assert result.status == "requires_approval"
    assert str(result.result["structured_data"]["amount"]) == "1500"
    # El único execute es el del vertical del tenant: NO se consultó el catálogo.
    assert mock_db.execute.await_count == 1


@pytest.mark.asyncio
async def test_llm_invents_amount_but_message_has_no_monetary_signal_uses_catalog():
    """LLM devuelve amount=30000 pero el mensaje no tiene monto →
    ignora 30000, busca en catálogo."""
    from app.application.agents.income.agent import AgentIncome

    mock_entities = {
        "amount": 30000,
        "quantity": 3,
        "date": "hoy",
        "payment_status": "paid",
        "payment_method": "efectivo",
        "product_description": "coca cola",
        "confidence": "HIGH",
    }
    mock_product = MagicMock()
    mock_product.sale_price_ars = Decimal("500")
    mock_product.name = "Coca-Cola 600ml"
    mock_product.id = "00000000-0000-0000-0000-000000000099"

    none_result = MagicMock()
    none_result.scalar_one_or_none.return_value = None
    none_result.scalars.return_value.all.return_value = []

    found_result = MagicMock()
    found_result.scalars.return_value.all.return_value = [mock_product]

    mock_db = _db_with_vertical(none_result, none_result, found_result)

    with unittest.mock.patch(
        "app.application.agents.income.agent.anthropic.AsyncAnthropic"
    ) as mock_cls:
        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(return_value=_mock_llm_response(mock_entities))
        mock_cls.return_value = mock_client

        agent = AgentIncome(db=mock_db)
        result = await agent.process(_make_request("vendí 3 coca colas"))

    assert result.status == "requires_approval"
    data = result.result["structured_data"]
    assert str(data["amount"]) == "1500"
    assert data["price_lookup_source"] == "products_db"


@pytest.mark.asyncio
async def test_llm_invents_amount_but_message_only_has_year_uses_catalog():
    """Un año en el mensaje no cuenta como monto explícito."""
    from app.application.agents.income.agent import AgentIncome

    mock_entities = {
        "amount": 2026,
        "quantity": 3,
        "date": "2026-04-20",
        "payment_status": "paid",
        "payment_method": "efectivo",
        "product_description": "coca cola",
        "confidence": "HIGH",
    }
    mock_product = MagicMock()
    mock_product.sale_price_ars = Decimal("500")
    mock_product.name = "Coca-Cola 600ml"
    mock_product.id = "00000000-0000-0000-0000-000000000099"

    none_result = MagicMock()
    none_result.scalar_one_or_none.return_value = None
    none_result.scalars.return_value.all.return_value = []

    found_result = MagicMock()
    found_result.scalars.return_value.all.return_value = [mock_product]

    mock_db = _db_with_vertical(none_result, none_result, found_result)

    with unittest.mock.patch(
        "app.application.agents.income.agent.anthropic.AsyncAnthropic"
    ) as mock_cls:
        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(return_value=_mock_llm_response(mock_entities))
        mock_cls.return_value = mock_client

        agent = AgentIncome(db=mock_db)
        result = await agent.process(_make_request("vendí 3 coca colas el 20 de abril de 2026"))

    assert result.status == "requires_approval"
    data = result.result["structured_data"]
    assert str(data["amount"]) == "1500"
    assert data["price_lookup_source"] == "products_db"


@pytest.mark.asyncio
async def test_product_ambiguous_returns_clarification_with_partial():
    """Producto ambiguo → requires_clarification con partial y lista de opciones."""
    from app.application.agents.income.agent import AgentIncome

    mock_entities = {
        "amount": None,
        "quantity": 1,
        "date": "hoy",
        "payment_status": "paid",
        "payment_method": "efectivo",
        "product_description": "coca",
        "confidence": "HIGH",
    }
    prod_a = MagicMock()
    prod_a.name = "Coca-Cola 600ml"
    prod_b = MagicMock()
    prod_b.name = "Coca-Cola 1.5L"

    none_result = MagicMock()
    none_result.scalar_one_or_none.return_value = None

    multi_result = MagicMock()
    multi_result.scalars.return_value.all.return_value = [prod_a, prod_b]

    mock_db = _db_with_vertical(none_result, none_result, multi_result)

    with unittest.mock.patch(
        "app.application.agents.income.agent.anthropic.AsyncAnthropic"
    ) as mock_cls:
        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(return_value=_mock_llm_response(mock_entities))
        mock_cls.return_value = mock_client

        agent = AgentIncome(db=mock_db)
        result = await agent.process(_make_request("vendí una coca"))

    assert result.status == "requires_clarification"
    assert "Coca-Cola 600ml" in result.question
    assert "Coca-Cola 1.5L" in result.question


@pytest.mark.asyncio
async def test_missing_payment_method_returns_clarification_with_partial():
    """LLM no detecta medio de pago → requires_clarification con partial."""
    from app.application.agents.income.agent import AgentIncome

    mock_entities = {
        "amount": 500,
        "quantity": 1,
        "date": "hoy",
        "payment_status": "paid",
        "payment_method": None,
        "product_description": "gaseosa",
        "confidence": "HIGH",
    }
    with unittest.mock.patch(
        "app.application.agents.income.agent.anthropic.AsyncAnthropic"
    ) as mock_cls:
        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(return_value=_mock_llm_response(mock_entities))
        mock_cls.return_value = mock_client

        agent = AgentIncome(default_vertical=Vertical.KIOSCO_ALMACEN)
        result = await agent.process(_make_request("vendí una gaseosa a $500"))

    assert result.status == "requires_clarification"
    assert "medio de pago" in result.question.lower() or "pago" in result.question.lower()
    partial = result.result.get("partial")
    assert partial is not None
    assert str(partial.get("amount")) == "500"


# ── Tests de AgentExpense (lógica de egresos) ─────────────────────────────────


@pytest.mark.asyncio
async def test_expense_message_is_auto_executed_candidate():
    """Gasto operativo → action_type=REGISTER_EXPENSE, auto_execute=True."""
    from app.application.agents.expense.agent import AgentExpense

    agent = AgentExpense()
    result = await agent.process(_make_request("Pagué alquiler $450.000 por transferencia"))

    assert result.status == "success"
    assert result.requires_approval is False
    assert result.risk_level == RiskLevel.MEDIUM
    assert result.result["action_type"] == ActionType.REGISTER_EXPENSE
    assert result.result["auto_execute"] is True
    assert result.result["structured_data"]["category"] == "RENT"
    assert result.result["structured_data"]["payment_method"] == "transfer"


# ── Tests del shim AgentCash ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cash_shim_dispatches_expense_to_expense_agent():
    """AgentCash despacha mensajes de gasto a AgentExpense."""
    from app.application.agents.cash.agent import AgentCash

    agent = AgentCash()
    result = await agent.process(_make_request("Pagué alquiler $450.000 por transferencia"))

    assert result.result["action_type"] == ActionType.REGISTER_EXPENSE
    # El shim preserva "agent_cash" en agent_name para audit log legacy
    assert result.agent_name == "agent_cash"


@pytest.mark.asyncio
async def test_cash_shim_dispatches_sale_to_income_agent():
    """AgentCash despacha mensajes de venta a AgentIncome."""
    mock_entities = {
        "amount": 5000,
        "date": "hoy",
        "payment_status": "paid",
        "payment_method": "efectivo",
        "product_description": None,
        "confidence": "HIGH",
    }
    with unittest.mock.patch(
        "app.application.agents.income.agent.anthropic.AsyncAnthropic"
    ) as mock_cls:
        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(return_value=_mock_llm_response(mock_entities))
        mock_cls.return_value = mock_client

        from app.application.agents.cash.agent import AgentCash

        agent = AgentCash(default_vertical=Vertical.KIOSCO_ALMACEN)
        result = await agent.process(_make_request("vendí 5000 al contado"))

    assert result.result["action_type"] == "REGISTER_SALE"
    assert result.agent_name == "agent_cash"  # preservado por el shim


@pytest.mark.asyncio
async def test_sale_emits_event_after_confirm():
    """on_confirmed_sale → EventBus emite SALE_RECORDED con la clave `tenant_id` (el
    task `events.sale_recorded` la lee así; emitir `business_id` dejaba el descuento
    de stock muerto)."""
    with unittest.mock.patch("app.application.agents.cash.agent.EventBus.emit") as mock_emit:
        from app.application.agents.cash.agent import AgentCash

        agent = AgentCash()
        await agent.on_confirmed_sale("sale-001", "tenant-001")

    mock_emit.assert_any_call("SALE_RECORDED", {"sale_id": "sale-001", "tenant_id": "tenant-001"})


@pytest.mark.asyncio
async def test_google_sheet_import_returns_pending_action_without_llm():
    """Google Sheets import → lee filas, parsea ventas y requiere aprobación."""
    from app.application.agents.cash.agent import AgentCash

    class FakeGateway:
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

    agent = AgentCash(gateway=FakeGateway())
    result = await agent.process(
        _make_request("Importa ventas desde https://docs.google.com/spreadsheets/d/sheet123/edit")
    )

    assert result.status == "requires_approval"
    assert result.result["action_type"] == ActionType.IMPORT_TABULAR_FILE
    payload = result.result["structured_data"]
    assert payload["source"] == "google_sheets"
