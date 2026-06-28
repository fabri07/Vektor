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

import json
from datetime import date
from decimal import Decimal
from uuid import UUID
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
    # El repo fue llamado con el tenant correcto
    sr.get_sales_by_customer.assert_called_once_with(UUID(_TENANT_A))


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
    # El repo fue llamado con el tenant correcto
    sr.get_sales_by_customer.assert_called_once_with(UUID(_TENANT_A))


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
    # El repo fue llamado con el tenant correcto
    call_args = er.expenses_by_category.call_args
    assert call_args.args[0] == UUID(_TENANT_A)


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
        patch.object(agent, "_supplier_totals", new_callable=AsyncMock, return_value=suppliers_a) as mock_st,
        patch.object(agent, "_critical_stock_for_order", new_callable=AsyncMock, return_value=[]),
    ):
        resp_a = await agent.process(_req(tenant_id=_TENANT_A), task=_task("agent_supplier"))

    names = [s["name"] for s in resp_a.result["structured_data"]["proveedores"]]
    assert "Proveedor Alfa" in names
    assert "Proveedor Beta" not in names
    # El método fue llamado con el tenant correcto
    call_args = mock_st.call_args
    assert call_args.args[0] == UUID(_TENANT_A)


# ── Regresión: json.dumps(structured_data) no falla con datetime.date ────────


@pytest.mark.asyncio
async def test_client_ranking_date_is_json_serializable() -> None:
    """last_sale_date como datetime.date → structured_data serializable con json.dumps.

    Regresión F5b-1 Finding 1: rank_customers pasa last_sale_date como date object;
    json.dumps levantaba TypeError antes del fix.
    """
    from app.application.agents.client.agent import AgentClient

    mock_db = MagicMock()
    agent = AgentClient(db=mock_db)
    agent.client = _make_client()

    # last_sale_date como datetime.date real — el bug original
    sales_data = [
        {
            "customer_id": "a",
            "customer_name": "Ana",
            "total": 5000.0,
            "n_sales": 5,
            "last_sale_date": date(2026, 6, 15),  # objeto date, NO string
        },
        {
            "customer_id": "b",
            "customer_name": "Beto",
            "total": 12000.0,
            "n_sales": 3,
            "last_sale_date": None,  # puede ser None también
        },
    ]
    balances_data: list = []
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
    sd = resp.result["structured_data"]
    # No debe lanzar TypeError
    serialized = json.dumps(sd)
    assert serialized  # no vacío
    # last_sale_date de Ana debe ser string ISO
    top = sd["ranking_top"]
    ana = next((r for r in top if r.get("customer_name") == "Ana"), None)
    assert ana is not None
    assert ana["last_sale_date"] == "2026-06-15"
    # None se preserva como null
    beto = next((r for r in top if r.get("customer_name") == "Beto"), None)
    assert beto is not None
    assert beto["last_sale_date"] is None


@pytest.mark.asyncio
async def test_income_ranking_date_is_json_serializable() -> None:
    """last_sale_date como datetime.date en ranking_clientes → json.dumps no falla.

    Regresión F5b-1 Finding 1 (income domain).
    """
    from app.application.agents.income.agent import AgentIncome

    mock_db = MagicMock()
    agent = AgentIncome(db=mock_db)
    agent.client = _make_client()

    sales_by_customer = [
        {
            "customer_id": "m1",
            "customer_name": "Mario",
            "total": 15000.0,
            "n_sales": 10,
            "last_sale_date": date(2026, 5, 20),  # objeto date real
        }
    ]
    sales_by_product: list = []
    ticket_data = {"ticket_promedio": Decimal("1500.00"), "n_transacciones": 10}

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
    sd = resp.result["structured_data"]
    # No debe lanzar TypeError
    serialized = json.dumps(sd)
    assert serialized
    # last_sale_date de Mario debe ser string ISO
    mario = next((r for r in sd["ranking_clientes"] if r.get("customer_name") == "Mario"), None)
    assert mario is not None
    assert mario["last_sale_date"] == "2026-05-20"


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


# ── F5b-2: agent_stock ────────────────────────────────────────────────────────


def _task_stock() -> AgentTask:
    return AgentTask(agent="agent_stock", action_type=ActionType.ANSWER_DATA_QUERY, entities={})


@pytest.mark.asyncio
async def test_stock_con_datos_llama_narrador() -> None:
    """Con productos → structured_data correcto, 1 LLMCall, message del narrador."""
    from app.application.agents.stock.agent import AgentStock

    mock_db = MagicMock()
    agent = AgentStock(db=mock_db)
    mock_llm = _make_client()
    agent.client = mock_llm

    products_data = [
        {
            "product_id": "p1",
            "name": "Coca Cola 500ml",
            "stock_units": 0,
            "sale_price": 500.0,
            "unit_cost": 300.0,
            "margin_pct": 40.0,
            "margin_abs": 200.0,
            "sku": "CC500",
            "category": "BEVERAGES",
        },
        {
            "product_id": "p2",
            "name": "Galletitas Oreo",
            "stock_units": 50,
            "sale_price": 400.0,
            "unit_cost": 200.0,
            "margin_pct": 50.0,
            "margin_abs": 200.0,
            "sku": "OREO",
            "category": "SNACKS",
        },
    ]
    velocity_data: dict = {"p1": 5.0, "p2": 0.5}

    with (
        patch(
            "app.persistence.repositories.product_repository.ProductRepository"
        ) as MockPR,
        patch(
            "app.persistence.repositories.transaction_repository.SaleRepository"
        ) as MockSR,
    ):
        pr = AsyncMock()
        pr.get_products_with_margin = AsyncMock(return_value=products_data)
        MockPR.return_value = pr

        sr = AsyncMock()
        sr.get_daily_velocity = AsyncMock(return_value=velocity_data)
        MockSR.return_value = sr

        resp = await agent.process(
            _req(message="¿qué productos me están por quedar sin stock?"),
            task=_task_stock(),
        )

    assert resp.status == "success"
    assert resp.message == _FakeContent.text
    assert resp.usage is not None
    assert len(resp.usage.calls) == 1
    assert resp.usage.calls[0].source == "data_query_narrator"

    sd = resp.result["structured_data"]
    assert "total_productos" in sd
    assert "stock_critico" in sd
    assert "margenes_top" in sd
    assert "sin_stock_n" in sd
    assert "sobrestock" in sd
    # Coca Cola sin stock → crítica
    assert sd["sin_stock_n"] == 1
    assert sd["total_productos"] == 2
    # Al menos Coca Cola en crítico
    criticos = [c["name"] for c in sd["stock_critico"]]
    assert "Coca Cola 500ml" in criticos
    # Oreo tiene margen 50% → en margenes_top
    assert len(sd["margenes_top"]) >= 1
    # Oreo: stock=50, velocity=0.5 → 100 días > 90 → sobrestock
    assert len(sd["sobrestock"]) >= 1
    sobrestock_names = [s["name"] for s in sd["sobrestock"]]
    assert "Galletitas Oreo" in sobrestock_names

    # No debe fallar json.dumps (serialización)
    assert json.dumps(sd)

    mock_llm.messages.create.assert_called_once()


@pytest.mark.asyncio
async def test_stock_sin_productos_no_llama_llm() -> None:
    """Sin productos → no-invention, SIN llamar al LLM."""
    from app.application.agents.stock.agent import AgentStock

    mock_db = MagicMock()
    agent = AgentStock(db=mock_db)
    mock_llm = _make_client()
    agent.client = mock_llm

    with patch(
        "app.persistence.repositories.product_repository.ProductRepository"
    ) as MockPR:
        pr = AsyncMock()
        pr.get_products_with_margin = AsyncMock(return_value=[])
        MockPR.return_value = pr

        resp = await agent.process(
            _req(message="¿qué stock tengo?"), task=_task_stock()
        )

    assert resp.status == "success"
    assert resp.usage is None
    mock_llm.messages.create.assert_not_called()
    assert "producto" in resp.message.lower()


@pytest.mark.asyncio
async def test_stock_sin_db_no_llama_llm() -> None:
    """Sin DB → no-invention sin llamar al LLM."""
    from app.application.agents.stock.agent import AgentStock

    agent = AgentStock(db=None)
    mock_llm = _make_client()
    agent.client = mock_llm

    resp = await agent.process(_req(message="¿cuánto stock tengo?"), task=_task_stock())

    assert resp.status == "success"
    assert resp.usage is None
    mock_llm.messages.create.assert_not_called()


@pytest.mark.asyncio
async def test_stock_cross_tenant_isolation() -> None:
    """structured_data de tenant A no incluye datos de tenant B."""
    from app.application.agents.stock.agent import AgentStock

    mock_db = MagicMock()
    agent = AgentStock(db=mock_db)
    mock_llm = _make_client()
    agent.client = mock_llm

    products_a = [
        {
            "product_id": "pa1",
            "name": "Producto Alpha",
            "stock_units": 10,
            "sale_price": 200.0,
            "unit_cost": 100.0,
            "margin_pct": 50.0,
            "margin_abs": 100.0,
            "sku": "PA1",
            "category": "OTHER",
        }
    ]

    with (
        patch(
            "app.persistence.repositories.product_repository.ProductRepository"
        ) as MockPR,
        patch(
            "app.persistence.repositories.transaction_repository.SaleRepository"
        ) as MockSR,
    ):
        pr = AsyncMock()
        pr.get_products_with_margin = AsyncMock(return_value=products_a)
        MockPR.return_value = pr

        sr = AsyncMock()
        sr.get_daily_velocity = AsyncMock(return_value={})
        MockSR.return_value = sr

        resp_a = await agent.process(_req(tenant_id=_TENANT_A), task=_task_stock())

    sd = resp_a.result["structured_data"]
    assert sd["total_productos"] == 1
    # El repo fue llamado con el tenant correcto
    pr.get_products_with_margin.assert_called_once_with(UUID(_TENANT_A))


@pytest.mark.asyncio
async def test_stock_structured_data_json_serializable() -> None:
    """structured_data de stock es JSON-serializable (sin Decimal ni date)."""
    from app.application.agents.stock.agent import AgentStock

    mock_db = MagicMock()
    agent = AgentStock(db=mock_db)
    agent.client = _make_client()

    products_data = [
        {
            "product_id": "p1",
            "name": "Producto Test",
            "stock_units": 0,
            "sale_price": 100.0,
            "unit_cost": 50.0,
            "margin_pct": 50.0,
            "margin_abs": 50.0,
            "sku": "PT",
            "category": "OTHER",
        }
    ]

    with (
        patch(
            "app.persistence.repositories.product_repository.ProductRepository"
        ) as MockPR,
        patch(
            "app.persistence.repositories.transaction_repository.SaleRepository"
        ) as MockSR,
    ):
        pr = AsyncMock()
        pr.get_products_with_margin = AsyncMock(return_value=products_data)
        MockPR.return_value = pr

        sr = AsyncMock()
        sr.get_daily_velocity = AsyncMock(return_value={})
        MockSR.return_value = sr

        resp = await agent.process(_req(), task=_task_stock())

    assert resp.status == "success"
    serialized = json.dumps(resp.result["structured_data"])
    assert serialized


def test_stock_has_client_property() -> None:
    """AgentStock tiene la property client con getter/setter."""
    from app.application.agents.stock.agent import AgentStock

    agent = AgentStock(db=None)
    mock = MagicMock()
    agent.client = mock
    assert agent.client is mock


# ── F5b-2: agent_marketing ────────────────────────────────────────────────────


def _task_marketing() -> AgentTask:
    return AgentTask(
        agent="agent_marketing", action_type=ActionType.ANSWER_DATA_QUERY, entities={}
    )


def _make_marketing_dashboard(has_data: bool = True) -> MagicMock:
    """Dashboard de marketing mockeado."""
    from decimal import Decimal

    dashboard = MagicMock()
    dashboard.has_data = has_data
    dashboard.days = 30
    dashboard.from_date = "2026-05-28"
    dashboard.to_date = "2026-06-27"
    dashboard.total_followers = 1500
    dashboard.total_reach = 8000
    dashboard.total_ads_spend_ars = Decimal("25000.00")

    platform = MagicMock()
    platform.platform = "instagram"
    platform.followers = 1500
    platform.reach = 8000
    platform.ads_spend_ars = Decimal("25000.00")
    dashboard.platforms = [platform]

    avs = MagicMock()
    avs.revenue_ars = Decimal("120000.00")
    avs.ads_spend_ars = Decimal("25000.00")
    avs.ratio = 0.208
    dashboard.ads_vs_sales = avs

    return dashboard


@pytest.mark.asyncio
async def test_marketing_con_datos_llama_narrador() -> None:
    """Con métricas → structured_data correcto, 1 LLMCall, message del narrador."""
    from app.application.agents.marketing.agent import AgentMarketing

    mock_db = MagicMock()
    agent = AgentMarketing(db=mock_db)
    mock_llm = _make_client()
    agent.client = mock_llm

    dashboard_mock = _make_marketing_dashboard(has_data=True)

    with patch(
        "app.application.services.marketing_service.MarketingService"
    ) as MockMS:
        ms = AsyncMock()
        ms.get_dashboard = AsyncMock(return_value=dashboard_mock)
        MockMS.return_value = ms

        resp = await agent.process(
            _req(message="¿cómo está mi marketing?"), task=_task_marketing()
        )

    assert resp.status == "success"
    assert resp.message == _FakeContent.text
    assert resp.usage is not None
    assert len(resp.usage.calls) == 1
    assert resp.usage.calls[0].source == "data_query_narrator"

    sd = resp.result["structured_data"]
    assert "has_data" in sd
    assert sd["has_data"] is True
    assert "total_followers" in sd
    assert sd["total_followers"] == 1500
    assert "total_ads_spend_ars" in sd
    assert sd["total_ads_spend_ars"] == pytest.approx(25000.0)
    assert isinstance(sd["total_ads_spend_ars"], float)
    assert "revenue_ars" in sd
    assert sd["revenue_ars"] == pytest.approx(120000.0)
    assert "plataformas" in sd
    assert sd["plataformas"][0]["platform"] == "instagram"

    # JSON-serializable
    assert json.dumps(sd)

    mock_llm.messages.create.assert_called_once()


@pytest.mark.asyncio
async def test_marketing_sin_datos_no_llama_llm() -> None:
    """Sin métricas (has_data=False) → no-invention, SIN llamar al LLM."""
    from app.application.agents.marketing.agent import AgentMarketing

    mock_db = MagicMock()
    agent = AgentMarketing(db=mock_db)
    mock_llm = _make_client()
    agent.client = mock_llm

    dashboard_mock = _make_marketing_dashboard(has_data=False)

    with patch(
        "app.application.services.marketing_service.MarketingService"
    ) as MockMS:
        ms = AsyncMock()
        ms.get_dashboard = AsyncMock(return_value=dashboard_mock)
        MockMS.return_value = ms

        resp = await agent.process(
            _req(message="¿cómo va mi publicidad?"), task=_task_marketing()
        )

    assert resp.status == "success"
    assert resp.usage is None
    mock_llm.messages.create.assert_not_called()
    assert "marketing" in resp.message.lower()


@pytest.mark.asyncio
async def test_marketing_sin_db_no_llama_llm() -> None:
    """Sin DB → no-invention sin llamar al LLM."""
    from app.application.agents.marketing.agent import AgentMarketing

    agent = AgentMarketing(db=None)
    mock_llm = _make_client()
    agent.client = mock_llm

    resp = await agent.process(_req(message="¿mi marketing?"), task=_task_marketing())

    assert resp.status == "success"
    assert resp.usage is None
    mock_llm.messages.create.assert_not_called()


@pytest.mark.asyncio
async def test_marketing_cross_tenant_isolation() -> None:
    """El servicio se llamó con el tenant_id correcto de tenant A."""
    from app.application.agents.marketing.agent import AgentMarketing

    mock_db = MagicMock()
    agent = AgentMarketing(db=mock_db)
    mock_llm = _make_client()
    agent.client = mock_llm

    dashboard_mock = _make_marketing_dashboard(has_data=True)

    with patch(
        "app.application.services.marketing_service.MarketingService"
    ) as MockMS:
        ms = AsyncMock()
        ms.get_dashboard = AsyncMock(return_value=dashboard_mock)
        MockMS.return_value = ms

        await agent.process(_req(tenant_id=_TENANT_A), task=_task_marketing())

    # get_dashboard llamado con el UUID de tenant A
    call_args = ms.get_dashboard.call_args
    assert call_args.args[0] == UUID(_TENANT_A)


def test_marketing_has_client_property() -> None:
    """AgentMarketing tiene la property client con getter/setter."""
    from app.application.agents.marketing.agent import AgentMarketing

    agent = AgentMarketing(db=None)
    mock = MagicMock()
    agent.client = mock
    assert agent.client is mock


@pytest.mark.asyncio
async def test_marketing_ratio_decimal_serializable() -> None:
    """ratio_ads_ventas como Decimal → structured_data JSON-serializable sin TypeError.

    Regresión F5b-2 Finding 2: avs.ratio devuelto como Decimal causa TypeError en json.dumps.
    """
    from app.application.agents.marketing.agent import AgentMarketing

    mock_db = MagicMock()
    agent = AgentMarketing(db=mock_db)
    agent.client = _make_client()

    # Dashboard con ratio como Decimal (el caso problemático)
    dashboard_mock = MagicMock()
    dashboard_mock.has_data = True
    dashboard_mock.days = 30
    dashboard_mock.from_date = "2026-05-28"
    dashboard_mock.to_date = "2026-06-27"
    dashboard_mock.total_followers = 2000
    dashboard_mock.total_reach = 10000
    dashboard_mock.total_ads_spend_ars = Decimal("30000.00")
    dashboard_mock.platforms = []

    avs = MagicMock()
    avs.revenue_ars = Decimal("144000.00")
    avs.ads_spend_ars = Decimal("30000.00")
    avs.ratio = Decimal("0.208")  # ← Decimal, no float
    dashboard_mock.ads_vs_sales = avs

    with patch(
        "app.application.services.marketing_service.MarketingService"
    ) as MockMS:
        ms = AsyncMock()
        ms.get_dashboard = AsyncMock(return_value=dashboard_mock)
        MockMS.return_value = ms

        resp = await agent.process(
            _req(message="¿cuánto gasto en publicidad?"), task=_task_marketing()
        )

    assert resp.status == "success"
    sd = resp.result["structured_data"]
    # ratio_ads_ventas debe ser float, no Decimal
    assert isinstance(sd["ratio_ads_ventas"], float)
    assert sd["ratio_ads_ventas"] == pytest.approx(0.208)
    # El json.dumps no debe lanzar TypeError
    serialized = json.dumps(sd)
    assert serialized


# ── F5b-2: agent_health ───────────────────────────────────────────────────────


def _task_health() -> AgentTask:
    return AgentTask(agent="agent_health", action_type=ActionType.ANSWER_DATA_QUERY, entities={})


@pytest.mark.asyncio
async def test_health_con_datos_llama_narrador() -> None:
    """Con datos financieros → structured_data correcto, 1 LLMCall, message del narrador."""
    from app.application.agents.health.agent import AgentHealth

    mock_db = MagicMock()
    agent = AgentHealth(db=mock_db)
    mock_llm = _make_client()
    agent.client = mock_llm

    from decimal import Decimal

    financial_summary = {
        "estado": "OK",
        "flujo_neto_30d": {
            "total_ventas": Decimal("150000.00"),
            "total_gastos": Decimal("90000.00"),
            "flujo_neto": Decimal("60000.00"),
            "periodo_dias": 30,
            "desde": "2026-05-28",
            "hasta": "2026-06-27",
        },
        "margen": {
            "margen_pct": Decimal("40.00"),
            "margen_abs": Decimal("60000.00"),
            "sin_datos": False,
        },
        "ticket_promedio": {
            "ticket_promedio": Decimal("5000.00"),
            "n_transacciones": 30,
            "sin_datos": False,
        },
        "rotacion": {"sin_datos": True},
    }

    with patch(
        "app.application.services.deterministic_finance.get_financial_summary",
        new_callable=AsyncMock,
        return_value=financial_summary,
    ):
        resp = await agent.process(
            _req(message="¿cómo está mi caja?"), task=_task_health()
        )

    assert resp.status == "success"
    assert resp.message == _FakeContent.text
    assert resp.usage is not None
    assert len(resp.usage.calls) == 1
    assert resp.usage.calls[0].source == "data_query_narrator"

    sd = resp.result["structured_data"]
    assert "resumen_financiero" in sd
    rf = sd["resumen_financiero"]
    assert rf["total_ventas"] == pytest.approx(150000.0)
    assert rf["total_gastos"] == pytest.approx(90000.0)
    assert rf["flujo_neto"] == pytest.approx(60000.0)
    assert rf["margen_pct"] == pytest.approx(40.0)
    assert rf["ticket_promedio"] == pytest.approx(5000.0)
    assert rf["n_transacciones"] == 30
    # Todos float (no Decimal)
    assert isinstance(rf["total_ventas"], float)
    assert isinstance(rf["margen_pct"], float)

    # JSON-serializable (no Decimal)
    assert json.dumps(sd)

    mock_llm.messages.create.assert_called_once()


@pytest.mark.asyncio
async def test_health_sin_datos_no_llama_llm() -> None:
    """Sin datos financieros (estado=SIN_DATOS) → no-invention, SIN llamar al LLM."""
    from app.application.agents.health.agent import AgentHealth

    mock_db = MagicMock()
    agent = AgentHealth(db=mock_db)
    mock_llm = _make_client()
    agent.client = mock_llm

    financial_summary = {"estado": "SIN_DATOS", "mensaje": "sin datos cargados"}

    with patch(
        "app.application.services.deterministic_finance.get_financial_summary",
        new_callable=AsyncMock,
        return_value=financial_summary,
    ):
        resp = await agent.process(
            _req(message="¿cuánto vendo?"), task=_task_health()
        )

    assert resp.status == "success"
    assert resp.usage is None
    mock_llm.messages.create.assert_not_called()
    assert "30 días" in resp.message or "movimiento" in resp.message.lower()


@pytest.mark.asyncio
async def test_health_sin_db_no_llama_llm() -> None:
    """Sin DB → no-invention sin llamar al LLM."""
    from app.application.agents.health.agent import AgentHealth

    agent = AgentHealth(db=None)
    mock_llm = _make_client()
    agent.client = mock_llm

    resp = await agent.process(_req(message="¿cómo está mi flujo?"), task=_task_health())

    assert resp.status == "success"
    assert resp.usage is None
    mock_llm.messages.create.assert_not_called()


@pytest.mark.asyncio
async def test_health_cross_tenant_isolation() -> None:
    """get_financial_summary se llamó con el UUID correcto del tenant A."""
    from app.application.agents.health.agent import AgentHealth

    mock_db = MagicMock()
    agent = AgentHealth(db=mock_db)
    mock_llm = _make_client()
    agent.client = mock_llm

    from decimal import Decimal

    financial_summary = {
        "estado": "OK",
        "flujo_neto_30d": {
            "total_ventas": Decimal("50000.00"),
            "total_gastos": Decimal("30000.00"),
            "flujo_neto": Decimal("20000.00"),
            "periodo_dias": 30,
            "desde": "2026-05-28",
            "hasta": "2026-06-27",
        },
        "margen": {"sin_datos": False, "margen_pct": Decimal("40.00")},
        "ticket_promedio": {"sin_datos": False, "ticket_promedio": Decimal("1000.00"), "n_transacciones": 50},
        "rotacion": {"sin_datos": True},
    }

    with patch(
        "app.application.services.deterministic_finance.get_financial_summary",
        new_callable=AsyncMock,
        return_value=financial_summary,
    ) as mock_fs:
        await agent.process(_req(tenant_id=_TENANT_A), task=_task_health())

    # Verificar que fue llamado con el tenant correcto
    mock_fs.assert_called_once_with(UUID(_TENANT_A), mock_db)


@pytest.mark.asyncio
async def test_health_structured_data_json_serializable() -> None:
    """structured_data de health es JSON-serializable (sin Decimal)."""
    from app.application.agents.health.agent import AgentHealth

    mock_db = MagicMock()
    agent = AgentHealth(db=mock_db)
    agent.client = _make_client()

    from decimal import Decimal

    financial_summary = {
        "estado": "OK",
        "flujo_neto_30d": {
            "total_ventas": Decimal("100000.00"),
            "total_gastos": Decimal("70000.00"),
            "flujo_neto": Decimal("30000.00"),
            "periodo_dias": 30,
            "desde": "2026-05-28",
            "hasta": "2026-06-27",
        },
        "margen": {"margen_pct": Decimal("30.00"), "sin_datos": False},
        "ticket_promedio": {"ticket_promedio": Decimal("3333.33"), "n_transacciones": 30, "sin_datos": False},
        "rotacion": {"sin_datos": True},
    }

    with patch(
        "app.application.services.deterministic_finance.get_financial_summary",
        new_callable=AsyncMock,
        return_value=financial_summary,
    ):
        resp = await agent.process(_req(message="¿cómo va el flujo?"), task=_task_health())

    assert resp.status == "success"
    # No debe lanzar TypeError por Decimal no serializable
    serialized = json.dumps(resp.result["structured_data"])
    assert serialized


def test_health_has_client_property() -> None:
    """AgentHealth tiene la property client con getter/setter."""
    from app.application.agents.health.agent import AgentHealth

    agent = AgentHealth(db=None)
    mock = MagicMock()
    agent.client = mock
    assert agent.client is mock
