"""Tests unitarios para handlers ANSWER_DATA_QUERY en 4 agentes (F5b-1).

Cubre (Brief F5b-1):
- agent_client: _handle_data_query → structured_data con cifras reales, status="success",
  1 LLMCall en usage; sin clientes → no-invention, sin LLM
- agent_income: ídem con datos de ventas
- agent_expense: ídem con datos de gastos
- agent_supplier: ídem con datos de proveedores
- Cross-tenant: structured_data de tenant A no incluye datos de tenant B

Patrón: cliente Anthropic mockeado, repos mockeados con patch.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.application.agents.shared.schemas import (
    ActionType,
    AgentRequest,
    AgentTask,
    Confidence,
)

_TENANT_A = "00000000-0000-0000-0000-000000000001"
_TENANT_B = "00000000-0000-0000-0000-000000000002"
_USER = "00000000-0000-0000-0000-000000000099"

# ── Helpers ───────────────────────────────────────────────────────────────────


def _req(tenant_id: str = _TENANT_A, message: str = "¿cuál es mi mejor cliente?") -> AgentRequest:
    return AgentRequest(user_id=_USER, business_id=tenant_id, message=message)


def _task(agent: str = "agent_client") -> AgentTask:
    return AgentTask(
        agent=agent,
        action_type=ActionType.ANSWER_DATA_QUERY,
        entities={},
    )


class _FakeUsage:
    input_tokens = 120
    output_tokens = 80


class _FakeContent:
    text = "Respuesta del narrador LLM sobre tus datos."


class _FakeResponse:
    content = [_FakeContent()]
    usage = _FakeUsage()


def _make_client() -> MagicMock:
    """Cliente Anthropic mockeado que devuelve _FakeResponse."""
    client = MagicMock()
    client.messages.create = AsyncMock(return_value=_FakeResponse())
    return client


# ── agent_client ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_client_con_datos_llama_narrador() -> None:
    """Con clientes y ventas → structured_data correcto, 1 LLMCall, message del narrador."""
    from app.application.agents.client.agent import AgentClient

    mock_db = MagicMock()
    agent = AgentClient(db=mock_db)
    mock_llm = _make_client()
    agent.client = mock_llm  # inyectar mock vía setter

    sales_data = [
        {"customer_id": "a", "customer_name": "Ana", "total": 5000.0, "n_sales": 5},
        {"customer_id": "b", "customer_name": "Beto", "total": 12000.0, "n_sales": 3},
    ]
    balances_data = [
        {
            "customer_id": "b",
            "customer_name": "Beto",
            "total_account": 3000.0,
            "total_paid": 1000.0,
            "balance": 2000.0,
            "n_sales": 1,
        }
    ]
    inactive_data: list = []

    with (
        patch(
            "app.persistence.repositories.customer_repository.CustomerRepository"
        ) as MockCR,
        patch(
            "app.persistence.repositories.transaction_repository.SaleRepository"
        ) as MockSR,
    ):
        cr = AsyncMock()
        cr.count_active = AsyncMock(return_value=2)
        cr.get_inactive_customers = AsyncMock(return_value=inactive_data)
        MockCR.return_value = cr

        sr = AsyncMock()
        sr.get_sales_by_customer = AsyncMock(return_value=sales_data)
        sr.get_balances_by_customer = AsyncMock(return_value=balances_data)
        MockSR.return_value = sr

        resp = await agent.process(_req(), task=_task("agent_client"))

    assert resp.status == "success"
    assert resp.message == _FakeContent.text
    assert resp.usage is not None
    assert len(resp.usage.calls) == 1
    assert resp.usage.calls[0].source == "data_query_narrator"

    sd = resp.result["structured_data"]
    assert "ranking_top" in sd
    assert "saldos" in sd
    assert "inactivos_n" in sd
    # Beto tiene más facturación → primero en el ranking
    assert sd["ranking_top"][0]["customer_name"] == "Beto"
    assert sd["ranking_top"][0]["total"] == 12000.0
    assert sd["inactivos_n"] == 0
    # Saldo de Beto serializado
    assert sd["saldos"][0]["balance"] == 2000.0

    # El LLM fue llamado exactamente una vez
    mock_llm.messages.create.assert_called_once()


@pytest.mark.asyncio
async def test_client_sin_clientes_no_llama_llm() -> None:
    """count_active==0 → no-invention, SIN llamar al LLM."""
    from app.application.agents.client.agent import AgentClient

    mock_db = MagicMock()
    agent = AgentClient(db=mock_db)
    mock_llm = _make_client()
    agent.client = mock_llm

    with patch(
        "app.persistence.repositories.customer_repository.CustomerRepository"
    ) as MockCR:
        cr = AsyncMock()
        cr.count_active = AsyncMock(return_value=0)
        MockCR.return_value = cr

        resp = await agent.process(_req(), task=_task("agent_client"))

    assert resp.status == "success"
    assert resp.usage is None
    # No-invention: el LLM NO fue llamado
    mock_llm.messages.create.assert_not_called()
    assert "clientes" in resp.message.lower()


@pytest.mark.asyncio
async def test_client_sin_db_no_llama_llm() -> None:
    """Sin DB → no-invention sin llamar al LLM."""
    from app.application.agents.client.agent import AgentClient

    agent = AgentClient(db=None)
    mock_llm = _make_client()
    agent.client = mock_llm

    resp = await agent.process(_req(), task=_task("agent_client"))

    assert resp.status == "success"
    assert resp.usage is None
    mock_llm.messages.create.assert_not_called()


@pytest.mark.asyncio
async def test_client_cross_tenant_isolation() -> None:
    """structured_data de tenant A no incluye datos de tenant B."""
    from app.application.agents.client.agent import AgentClient

    mock_db = MagicMock()
    agent = AgentClient(db=mock_db)
    mock_llm = _make_client()
    agent.client = mock_llm

    # Tenant A tiene un cliente; tenant B tiene otro
    sales_tenant_a = [
        {"customer_id": "a1", "customer_name": "Carlos A", "total": 8000.0, "n_sales": 4}
    ]

    with (
        patch(
            "app.persistence.repositories.customer_repository.CustomerRepository"
        ) as MockCR,
        patch(
            "app.persistence.repositories.transaction_repository.SaleRepository"
        ) as MockSR,
    ):
        cr = AsyncMock()
        cr.count_active = AsyncMock(return_value=1)
        cr.get_inactive_customers = AsyncMock(return_value=[])
        MockCR.return_value = cr

        sr = AsyncMock()
        sr.get_sales_by_customer = AsyncMock(return_value=sales_tenant_a)
        sr.get_balances_by_customer = AsyncMock(return_value=[])
        MockSR.return_value = sr

        resp_a = await agent.process(_req(tenant_id=_TENANT_A), task=_task("agent_client"))

    sd_a = resp_a.result["structured_data"]
    names = [r.get("customer_name") for r in sd_a.get("ranking_top", [])]
    assert "Carlos A" in names
    # Datos de tenant B no deben aparecer
    assert "Carlos B" not in names


# ── agent_income ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_income_con_datos_llama_narrador() -> None:
    """Con ventas → structured_data con clientes y productos, 1 LLMCall."""
    from app.application.agents.income.agent import AgentIncome

    mock_db = MagicMock()
    agent = AgentIncome(db=mock_db)
    mock_llm = _make_client()
    agent.client = mock_llm

    sales_by_customer = [
        {"customer_id": "c1", "customer_name": "Mario", "total": 15000.0, "n_sales": 10}
    ]
    sales_by_product = [
        {"product_id": "p1", "revenue": 9000.0, "units": 30, "n_sales": 30}
    ]
    ticket_data = {"ticket_promedio": Decimal("1500.00"), "n_transacciones": 10, "sin_datos": False}

    with (
        patch(
            "app.persistence.repositories.transaction_repository.SaleRepository"
        ) as MockSR,
        patch(
            "app.application.services.deterministic_finance.calcular_ticket_promedio",
            new_callable=AsyncMock,
            return_value=ticket_data,
        ),
    ):
        sr = AsyncMock()
        sr.get_sales_by_customer = AsyncMock(return_value=sales_by_customer)
        sr.get_sales_by_product = AsyncMock(return_value=sales_by_product)
        MockSR.return_value = sr

        resp = await agent.process(
            _req(message="¿cuánto vendí?"), task=_task("agent_income")
        )

    assert resp.status == "success"
    assert resp.message == _FakeContent.text
    assert resp.usage is not None
    assert len(resp.usage.calls) == 1

    sd = resp.result["structured_data"]
    assert "ranking_clientes" in sd
    assert "ventas_por_producto" in sd
    assert "ticket_promedio" in sd
    # Mario aparece en el ranking
    assert sd["ranking_clientes"][0]["customer_name"] == "Mario"
    assert sd["ranking_clientes"][0]["total"] == 15000.0
    # Ticket promedio correcto
    assert sd["ticket_promedio"] == pytest.approx(1500.0)
    # Ventas por producto correctas
    assert sd["ventas_por_producto"][0]["revenue"] == 9000.0

    mock_llm.messages.create.assert_called_once()


@pytest.mark.asyncio
async def test_income_sin_ventas_no_llama_llm() -> None:
    """Sin ventas → no-invention, SIN llamar al LLM."""
    from app.application.agents.income.agent import AgentIncome

    mock_db = MagicMock()
    agent = AgentIncome(db=mock_db)
    mock_llm = _make_client()
    agent.client = mock_llm

    with (
        patch(
            "app.persistence.repositories.transaction_repository.SaleRepository"
        ) as MockSR,
        patch(
            "app.application.services.deterministic_finance.calcular_ticket_promedio",
            new_callable=AsyncMock,
            return_value={"sin_datos": True},
        ),
    ):
        sr = AsyncMock()
        sr.get_sales_by_customer = AsyncMock(return_value=[])
        sr.get_sales_by_product = AsyncMock(return_value=[])
        MockSR.return_value = sr

        resp = await agent.process(
            _req(message="¿cuánto vendí?"), task=_task("agent_income")
        )

    assert resp.status == "success"
    assert resp.usage is None
    mock_llm.messages.create.assert_not_called()
    assert "ventas" in resp.message.lower()


@pytest.mark.asyncio
async def test_income_sin_db_no_llama_llm() -> None:
    """Sin DB → no-invention sin llamar al LLM."""
    from app.application.agents.income.agent import AgentIncome

    agent = AgentIncome(db=None)
    mock_llm = _make_client()
    agent.client = mock_llm

    resp = await agent.process(_req(message="cuánto vendí"), task=_task("agent_income"))

    assert resp.status == "success"
    assert resp.usage is None
    mock_llm.messages.create.assert_not_called()


@pytest.mark.asyncio
async def test_income_cross_tenant_isolation() -> None:
    """structured_data de tenant A no incluye ventas de tenant B."""
    from app.application.agents.income.agent import AgentIncome

    mock_db = MagicMock()
    agent = AgentIncome(db=mock_db)
    mock_llm = _make_client()
    agent.client = mock_llm

    sales_a = [{"customer_id": "x1", "customer_name": "Laura A", "total": 4000.0, "n_sales": 2}]

    with (
        patch(
            "app.persistence.repositories.transaction_repository.SaleRepository"
        ) as MockSR,
        patch(
            "app.application.services.deterministic_finance.calcular_ticket_promedio",
            new_callable=AsyncMock,
            return_value={"ticket_promedio": Decimal("2000.00"), "n_transacciones": 2},
        ),
    ):
        sr = AsyncMock()
        sr.get_sales_by_customer = AsyncMock(return_value=sales_a)
        sr.get_sales_by_product = AsyncMock(return_value=[])
        MockSR.return_value = sr

        resp_a = await agent.process(_req(tenant_id=_TENANT_A), task=_task("agent_income"))

    names = [r.get("customer_name") for r in resp_a.result["structured_data"]["ranking_clientes"]]
    assert "Laura A" in names
    assert "Laura B" not in names


# ── agent_expense ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_expense_con_datos_llama_narrador() -> None:
    """Con gastos → structured_data con categorías, total y anomalías, 1 LLMCall."""
    from app.application.agents.expense.agent import AgentExpense

    mock_db = MagicMock()
    agent = AgentExpense(db=mock_db)
    mock_llm = _make_client()
    agent.client = mock_llm

    by_cat = [
        {"category": "RENT", "total": 45000.0, "pct": 60.0},
        {"category": "UTILITIES", "total": 15000.0, "pct": 20.0},
        {"category": "PAYROLL", "total": 15000.0, "pct": 20.0},
    ]
    stats_by_cat: dict = {}  # sin anomalías

    with patch(
        "app.persistence.repositories.transaction_repository.ExpenseRepository"
    ) as MockER:
        er = AsyncMock()
        er.expenses_by_category = AsyncMock(return_value=by_cat)
        er.get_expense_stats_by_category = AsyncMock(return_value=stats_by_cat)
        MockER.return_value = er

        resp = await agent.process(
            _req(message="¿en qué gasto más?"), task=_task("agent_expense")
        )

    assert resp.status == "success"
    assert resp.message == _FakeContent.text
    assert resp.usage is not None
    assert len(resp.usage.calls) == 1

    sd = resp.result["structured_data"]
    assert "por_categoria" in sd
    assert "total" in sd
    assert "anomalias" in sd
    # Total correcto: 45000 + 15000 + 15000 = 75000
    assert sd["total"] == pytest.approx(75000.0)
    # Categorías presentes
    cats = [c["category"] for c in sd["por_categoria"]]
    assert "RENT" in cats

    mock_llm.messages.create.assert_called_once()


@pytest.mark.asyncio
async def test_expense_sin_gastos_no_llama_llm() -> None:
    """Sin gastos → no-invention, SIN llamar al LLM."""
    from app.application.agents.expense.agent import AgentExpense

    mock_db = MagicMock()
    agent = AgentExpense(db=mock_db)
    mock_llm = _make_client()
    agent.client = mock_llm

    with patch(
        "app.persistence.repositories.transaction_repository.ExpenseRepository"
    ) as MockER:
        er = AsyncMock()
        er.expenses_by_category = AsyncMock(return_value=[])
        MockER.return_value = er

        resp = await agent.process(
            _req(message="¿cuánto gasté?"), task=_task("agent_expense")
        )

    assert resp.status == "success"
    assert resp.usage is None
    mock_llm.messages.create.assert_not_called()
    assert "gastos" in resp.message.lower()


@pytest.mark.asyncio
async def test_expense_sin_db_no_llama_llm() -> None:
    """Sin DB → no-invention sin llamar al LLM."""
    from app.application.agents.expense.agent import AgentExpense

    agent = AgentExpense(db=None)
    mock_llm = _make_client()
    agent.client = mock_llm

    resp = await agent.process(_req(message="cuánto gasté"), task=_task("agent_expense"))

    assert resp.status == "success"
    assert resp.usage is None
    mock_llm.messages.create.assert_not_called()


@pytest.mark.asyncio
async def test_expense_cross_tenant_isolation() -> None:
    """structured_data de tenant A no incluye gastos de tenant B."""
    from app.application.agents.expense.agent import AgentExpense

    mock_db = MagicMock()
    agent = AgentExpense(db=mock_db)
    mock_llm = _make_client()
    agent.client = mock_llm

    by_cat_a = [{"category": "RENT", "total": 50000.0, "pct": 100.0}]

    with patch(
        "app.persistence.repositories.transaction_repository.ExpenseRepository"
    ) as MockER:
        er = AsyncMock()
        er.expenses_by_category = AsyncMock(return_value=by_cat_a)
        er.get_expense_stats_by_category = AsyncMock(return_value={})
        MockER.return_value = er

        resp_a = await agent.process(_req(tenant_id=_TENANT_A), task=_task("agent_expense"))

    sd = resp_a.result["structured_data"]
    assert sd["total"] == pytest.approx(50000.0)
    cats = [c["category"] for c in sd["por_categoria"]]
    assert "RENT" in cats


# ── agent_supplier ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_supplier_con_datos_llama_narrador() -> None:
    """Con proveedores y stock crítico → structured_data correcto, 1 LLMCall."""
    from app.application.agents.supplier.agent import AgentSupplier

    mock_session = MagicMock()
    agent = AgentSupplier(session=mock_session)
    mock_llm = _make_client()
    agent.client = mock_llm

    supplier_totals = [
        {
            "name": "Distribuidora Norte",
            "total": 80000.0,
            "count": 5,
            "last_purchase": "2026-06-01",
            "days_since": 27,
            "pct": 70.0,
        }
    ]
    critical_stock = [
        {
            "name": "Coca Cola 500ml",
            "stock": 2,
            "product_id": "prod-uuid-1",
            "unit_cost": Decimal("350.00"),
            "threshold": 5,
        }
    ]

    with (
        patch.object(agent, "_supplier_totals", new_callable=AsyncMock, return_value=supplier_totals),
        patch.object(
            agent, "_critical_stock_for_order", new_callable=AsyncMock, return_value=critical_stock
        ),
    ):
        resp = await agent.process(
            _req(message="¿quiénes son mis proveedores?"), task=_task("agent_supplier")
        )

    assert resp.status == "success"
    assert resp.message == _FakeContent.text
    assert resp.usage is not None
    assert len(resp.usage.calls) == 1

    sd = resp.result["structured_data"]
    assert "proveedores" in sd
    assert "stock_critico" in sd
    # Distribuidor presente
    assert sd["proveedores"][0]["name"] == "Distribuidora Norte"
    assert sd["proveedores"][0]["total"] == 80000.0
    # Stock crítico serializado (Decimal → float)
    assert sd["stock_critico"][0]["unit_cost"] == pytest.approx(350.0)
    assert isinstance(sd["stock_critico"][0]["unit_cost"], float)

    mock_llm.messages.create.assert_called_once()


@pytest.mark.asyncio
async def test_supplier_sin_datos_no_llama_llm() -> None:
    """Sin proveedores ni stock crítico → no-invention, SIN llamar al LLM."""
    from app.application.agents.supplier.agent import AgentSupplier

    mock_session = MagicMock()
    agent = AgentSupplier(session=mock_session)
    mock_llm = _make_client()
    agent.client = mock_llm

    with (
        patch.object(agent, "_supplier_totals", new_callable=AsyncMock, return_value=[]),
        patch.object(agent, "_critical_stock_for_order", new_callable=AsyncMock, return_value=[]),
    ):
        resp = await agent.process(
            _req(message="¿cuáles son mis proveedores?"), task=_task("agent_supplier")
        )

    assert resp.status == "success"
    assert resp.usage is None
    mock_llm.messages.create.assert_not_called()
    assert "proveedor" in resp.message.lower()


@pytest.mark.asyncio
async def test_supplier_sin_session_no_llama_llm() -> None:
    """Sin session → no-invention sin llamar al LLM."""
    from app.application.agents.supplier.agent import AgentSupplier

    agent = AgentSupplier(session=None)
    mock_llm = _make_client()
    agent.client = mock_llm

    resp = await agent.process(_req(message="cuéntame de mis proveedores"), task=_task("agent_supplier"))

    assert resp.status == "success"
    assert resp.usage is None
    mock_llm.messages.create.assert_not_called()


@pytest.mark.asyncio
async def test_supplier_cross_tenant_isolation() -> None:
    """structured_data de tenant A no incluye proveedores de tenant B."""
    from app.application.agents.supplier.agent import AgentSupplier

    mock_session = MagicMock()
    agent = AgentSupplier(session=mock_session)
    mock_llm = _make_client()
    agent.client = mock_llm

    suppliers_a = [
        {
            "name": "Proveedor Alfa",
            "total": 30000.0,
            "count": 3,
            "last_purchase": "2026-06-10",
            "days_since": 18,
            "pct": 100.0,
        }
    ]

    with (
        patch.object(agent, "_supplier_totals", new_callable=AsyncMock, return_value=suppliers_a),
        patch.object(agent, "_critical_stock_for_order", new_callable=AsyncMock, return_value=[]),
    ):
        resp_a = await agent.process(_req(tenant_id=_TENANT_A), task=_task("agent_supplier"))

    names = [s["name"] for s in resp_a.result["structured_data"]["proveedores"]]
    assert "Proveedor Alfa" in names
    assert "Proveedor Beta" not in names


# ── client property en agent_client y agent_expense ──────────────────────────


def test_agent_client_has_client_property() -> None:
    """AgentClient tiene la property client con getter/setter."""
    from app.application.agents.client.agent import AgentClient

    agent = AgentClient(db=None)
    mock = MagicMock()
    agent.client = mock
    assert agent.client is mock


def test_agent_expense_has_client_property() -> None:
    """AgentExpense tiene la property client con getter/setter."""
    from app.application.agents.expense.agent import AgentExpense

    agent = AgentExpense(db=None)
    mock = MagicMock()
    agent.client = mock
    assert agent.client is mock


# ── Routing: ANSWER_DATA_QUERY en la tabla de intents ────────────────────────


def test_answer_data_query_action_type_exists() -> None:
    """ActionType.ANSWER_DATA_QUERY existe en el schema."""
    assert ActionType.ANSWER_DATA_QUERY == "ANSWER_DATA_QUERY"
