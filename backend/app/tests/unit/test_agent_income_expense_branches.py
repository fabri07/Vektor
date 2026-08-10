"""Tests para los branches REGISTER_CASH_INFLOW y REGISTER_CASH_OUTFLOW (Stage 2)
y para AgentGoogle con task dispatch (Stage 2).
"""

import json
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock

from app.application.agents.shared.schemas import ActionType, AgentRequest, AgentTask

_TENANT = "00000000-0000-0000-0000-000000000001"
_USER = "00000000-0000-0000-0000-000000000002"


def _req(message: str) -> AgentRequest:
    return AgentRequest(user_id=_USER, business_id=_TENANT, message=message)


def _task(action_type: ActionType, entities: dict[str, Any] | None = None) -> AgentTask:
    return AgentTask(
        agent="agent_income"
        if "INFLOW" in str(action_type) or "SALE" in str(action_type)
        else "agent_expense",
        action_type=action_type,
        entities=entities or {},
    )


# ── AgentIncome — REGISTER_CASH_INFLOW ───────────────────────────────────────


async def test_income_cash_inflow_with_amount_in_message():
    """Cobro con monto en el mensaje → requires_approval con REGISTER_CASH_INFLOW."""
    from app.application.agents.income.agent import AgentIncome

    agent = AgentIncome()
    task = _task(ActionType.REGISTER_CASH_INFLOW)
    result = await agent.process(_req("cobré $15.000 de la deuda de Juan"), task=task)

    assert result.status == "requires_approval"
    assert result.result["action_type"] == ActionType.REGISTER_CASH_INFLOW
    assert result.agent_name == "agent_income"
    assert Decimal(result.result["structured_data"]["amount"]) > 0


async def test_income_cash_inflow_from_task_entities():
    """Cobro con entities pre-provistas por el CEO → usa los datos del task."""
    from app.application.agents.income.agent import AgentIncome

    agent = AgentIncome()
    task = _task(
        ActionType.REGISTER_CASH_INFLOW,
        {
            "amount": "8000",
            "payment_method": "transfer",
            "description": "Cobro deuda cliente X",
        },
    )
    result = await agent.process(_req("cobré lo de Juan"), task=task)

    assert result.status == "requires_approval"
    assert result.result["action_type"] == ActionType.REGISTER_CASH_INFLOW
    data = result.result["structured_data"]
    assert data["amount"] == "8000"


async def test_income_cash_inflow_resolves_customer_name_to_id(monkeypatch):
    """Regresión (review C5): el cobro con `cliente` (nombre, lo que extrae el CEO)
    resuelve el customer_id vía el repo y lo inyecta en structured_data.
    """
    import uuid as _uuid

    from app.application.agents.income.agent import AgentIncome
    from app.persistence.repositories import customer_repository as _cr

    _cid = _uuid.uuid4()
    _fake_customer = MagicMock()
    _fake_customer.id = _cid

    async def _fake_find_by_name(self, name_normalized, tenant_id):  # noqa: ANN001
        return _fake_customer if name_normalized == "juan" else None

    monkeypatch.setattr(_cr.CustomerRepository, "find_by_name", _fake_find_by_name)

    agent = AgentIncome(db=MagicMock())
    task = _task(ActionType.REGISTER_CASH_INFLOW, {"amount": "5000", "cliente": "Juan"})
    result = await agent.process(_req("cobré $5000 a Juan"), task=task)

    assert result.result["structured_data"]["customer_id"] == str(_cid)


async def test_income_cash_inflow_unknown_customer_name_leaves_unlinked(monkeypatch):
    """Si el nombre no matchea ningún cliente, el cobro NO se bloquea y queda sin vincular."""
    from app.application.agents.income.agent import AgentIncome
    from app.persistence.repositories import customer_repository as _cr

    async def _fake_find_by_name(self, name_normalized, tenant_id):  # noqa: ANN001
        return None

    monkeypatch.setattr(_cr.CustomerRepository, "find_by_name", _fake_find_by_name)

    agent = AgentIncome(db=MagicMock())
    task = _task(ActionType.REGISTER_CASH_INFLOW, {"amount": "5000", "cliente": "Inexistente"})
    result = await agent.process(_req("cobré $5000 a Inexistente"), task=task)

    assert result.status == "requires_approval"
    assert "customer_id" not in result.result["structured_data"]


async def test_income_cash_inflow_without_amount_asks_clarification():
    """Cobro sin monto en el mensaje ni en task → requires_clarification."""
    from app.application.agents.income.agent import AgentIncome

    agent = AgentIncome()
    task = _task(ActionType.REGISTER_CASH_INFLOW)
    result = await agent.process(_req("cobré algo de la deuda de Juan"), task=task)

    assert result.status == "requires_clarification"
    assert "monto" in result.question.lower() or "cuánto" in result.question.lower()


async def test_income_register_sale_with_entity_injection():
    """REGISTER_SALE con entities pre-provistas (amount + payment_method) → salta el LLM."""
    from app.application.agents.income.agent import AgentIncome

    # Con entities completas, AgentIncome no llama al LLM y va directo a requires_approval
    task = AgentTask(
        agent="agent_income",
        action_type=ActionType.REGISTER_SALE,
        entities={
            "amount": "2000",
            "payment_method": "efectivo",
            "payment_status": "paid",
            "quantity": 1,
        },
    )
    agent = AgentIncome()
    result = await agent.process(_req("vendí 2000 al contado"), task=task)

    assert result.status == "requires_approval"
    assert result.result["action_type"] == ActionType.REGISTER_SALE
    assert result.agent_name == "agent_income"


# ── AgentExpense — REGISTER_CASH_OUTFLOW ─────────────────────────────────────


async def test_expense_cash_outflow_with_amount_in_message():
    """Pago salida con monto en mensaje → requires_approval con REGISTER_CASH_OUTFLOW."""
    from app.application.agents.expense.agent import AgentExpense

    agent = AgentExpense()
    task = _task(ActionType.REGISTER_CASH_OUTFLOW)
    result = await agent.process(_req("pagué $20.000 al proveedor Fernández"), task=task)

    assert result.status == "requires_approval"
    assert result.result["action_type"] == ActionType.REGISTER_CASH_OUTFLOW
    assert result.agent_name == "agent_expense"
    from decimal import Decimal

    assert Decimal(result.result["structured_data"]["amount"]) > 0


async def test_expense_cash_outflow_from_task_entities():
    """Pago salida con entities del CEO → usa monto provisto."""
    from app.application.agents.expense.agent import AgentExpense

    agent = AgentExpense()
    task = _task(
        ActionType.REGISTER_CASH_OUTFLOW,
        {
            "amount": "50000",
            "description": "Pago proveedor Mayorista",
            "payment_method": "transfer",
        },
    )
    result = await agent.process(_req("pagué al mayorista"), task=task)

    assert result.status == "requires_approval"
    assert result.result["action_type"] == ActionType.REGISTER_CASH_OUTFLOW
    assert result.result["structured_data"]["amount"] == "50000"


async def test_expense_cash_outflow_without_amount_asks_clarification():
    """Pago salida sin monto → requires_clarification."""
    from app.application.agents.expense.agent import AgentExpense

    agent = AgentExpense()
    task = _task(ActionType.REGISTER_CASH_OUTFLOW)
    result = await agent.process(_req("pagué algo al proveedor"), task=task)

    assert result.status == "requires_clarification"
    assert "monto" in result.question.lower() or "pago" in result.question.lower()


async def test_expense_register_expense_remains_default_branch():
    """REGISTER_EXPENSE sigue siendo el branch por defecto de AgentExpense."""
    from app.application.agents.expense.agent import AgentExpense

    agent = AgentExpense()
    task = _task(ActionType.REGISTER_EXPENSE)
    result = await agent.process(_req("pagué el alquiler $450.000 por transferencia"), task=task)

    assert result.result["action_type"] == ActionType.REGISTER_EXPENSE
    assert result.result["auto_execute"] is True


async def test_expense_outflow_is_requires_approval_not_auto_execute():
    """REGISTER_CASH_OUTFLOW requiere aprobación, a diferencia de REGISTER_EXPENSE."""
    from app.application.agents.expense.agent import AgentExpense

    agent = AgentExpense()
    task = _task(ActionType.REGISTER_CASH_OUTFLOW)
    result = await agent.process(_req("pagué $50.000 al proveedor"), task=task)

    assert result.requires_approval is True
    assert result.result.get("auto_execute") is None  # no es auto-execute


# ── AgentGoogle — dispatch con task ──────────────────────────────────────────


async def test_google_no_gateway_returns_auth_required():
    """AgentGoogle sin gateway → requires_google_auth (calendar handler)."""
    from app.application.agents.google.agent import AgentGoogle

    agent = AgentGoogle(gateway=None)
    result = await agent.process(_req("agendá una reunión"))
    assert result.status == "requires_google_auth"


async def test_google_with_calendar_task_no_gateway():
    """Con task CREATE_CALENDAR_EVENT y sin gateway → requires_google_auth."""
    from app.application.agents.google.agent import AgentGoogle

    agent = AgentGoogle(gateway=None)
    task = AgentTask(agent="agent_google", action_type=ActionType.CREATE_CALENDAR_EVENT)
    result = await agent.process(_req("agendá una reunión"), task=task)
    assert result.status == "requires_google_auth"
    assert result.agent_name == "agent_google"


async def test_google_without_task_defaults_to_sync_path():
    """Sin task (task=None), AgentGoogle defaultea al path de sync/Sheets."""
    from app.application.agents.google.agent import AgentGoogle

    agent = AgentGoogle(gateway=None)
    # Sin gateway y sin task → sync → requires_google_auth
    result = await agent.process(_req("sincronizá las ventas con Sheets"))

    assert result.status == "requires_google_auth"


# ── Helpers internos de los tests ─────────────────────────────────────────────


def _llm_resp(entities: dict[str, Any]) -> MagicMock:
    cb = MagicMock()
    cb.text = json.dumps(entities)
    r = MagicMock()
    r.content = [cb]
    r.usage = MagicMock(input_tokens=50, output_tokens=20)
    return r
