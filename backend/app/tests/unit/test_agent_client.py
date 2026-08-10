"""Tests unitarios para AgentClient (Véktor v4 — Fase 2 chat clientes).

Cubre (Brief F2b):
- agent_name == "agent_client"
- process() con ANALYZE_SALES_DATA + analizar_cuentas_por_cobrar → total adeudado correcto
- process() con analizar_clientes (default) + ventas → ranking top
- Sin clientes (count_active==0) → "todavía no tenés clientes", confidence MEDIUM, sin cifras
- Routing: INTENT_TO_AGENT["analizar_clientes"] == "agent_client"
- Routing: INTENT_TO_ACTION_TYPE["analizar_clientes"] == ANALYZE_SALES_DATA (sin cambios)
- registry.get_sub_agent("agent_client") → instancia de AgentClient
"""

from unittest.mock import AsyncMock, MagicMock, patch

from app.application.agents.shared.schemas import (
    ActionType,
    AgentRequest,
    AgentTask,
    Confidence,
)

_TENANT = "00000000-0000-0000-0000-000000000001"
_USER = "00000000-0000-0000-0000-000000000002"


def _req(tenant_id: str = _TENANT) -> AgentRequest:
    return AgentRequest(user_id=_USER, business_id=tenant_id, message="analizar clientes")


def _task(intent: str) -> AgentTask:
    return AgentTask(
        agent="agent_client",
        action_type=ActionType.ANALYZE_SALES_DATA,
        entities={"_intent": intent},
    )


# ── agent_name ────────────────────────────────────────────────────────────────


def test_agent_client_name():
    from app.application.agents.client.agent import AgentClient

    assert AgentClient().agent_name == "agent_client"


# ── Routing ───────────────────────────────────────────────────────────────────


def test_routing_analizar_clientes_to_agent_client():
    """analizar_clientes debe rutear a agent_client, no a agent_income."""
    from app.application.agents.ceo.team_plan_builder import INTENT_TO_AGENT

    assert INTENT_TO_AGENT["analizar_clientes"] == "agent_client"


def test_routing_analizar_clientes_action_type_unchanged():
    """analizar_clientes sigue usando ANALYZE_SALES_DATA — sin ActionType nuevo."""
    from app.application.agents.ceo.team_plan_builder import INTENT_TO_ACTION_TYPE

    assert INTENT_TO_ACTION_TYPE["analizar_clientes"] == ActionType.ANALYZE_SALES_DATA


def test_registry_agent_client_returns_instance():
    """registry.get_sub_agent('agent_client') devuelve AgentClient."""
    from app.application.agents.client.agent import AgentClient
    from app.application.agents.registry import get_sub_agent

    agent = get_sub_agent("agent_client", db=None)
    assert isinstance(agent, AgentClient)


# ── Sin DB → mensaje claro, no reventar ──────────────────────────────────────


async def test_sin_db_devuelve_mensaje_claro():
    """Sin DB → clientes_sin_datos, no reventar."""
    from app.application.agents.client.agent import AgentClient

    agent = AgentClient(db=None)
    resp = await agent.process(_req(), task=_task("analizar_clientes"))

    assert resp.status == "success"
    assert resp.result["summary"] == "clientes_sin_datos"
    assert "cliente" in resp.message.lower()


# ── count_active == 0 → no-invention ─────────────────────────────────────────


async def test_sin_clientes_cargados_no_inventa():
    """count_active==0 → "todavía no tenés clientes", confidence MEDIUM, sin cifras inventadas."""
    from app.application.agents.client.agent import AgentClient

    mock_db = MagicMock()
    agent = AgentClient(db=mock_db)

    with (
        patch(
            "app.persistence.repositories.customer_repository.CustomerRepository"
        ) as MockCR,
        patch("app.persistence.repositories.transaction_repository.SaleRepository"),
    ):
        cr_instance = AsyncMock()
        cr_instance.count_active = AsyncMock(return_value=0)
        MockCR.return_value = cr_instance

        resp = await agent.process(_req(), task=_task("analizar_clientes"))

    assert resp.status == "success"
    assert resp.result["summary"] == "clientes_sin_datos"
    assert resp.confidence == Confidence.MEDIUM
    # No-invention: el mensaje no debe contener cifras cuando no hay clientes
    assert "$" not in resp.message


# ── analizar_clientes (general) con ventas → ranking ─────────────────────────


async def test_analizar_clientes_ranking_top():
    """Con ventas disponibles → ranking por facturación, Beto primero (10.000 > 3.000)."""
    from app.application.agents.client.agent import AgentClient

    mock_db = MagicMock()
    agent = AgentClient(db=mock_db)

    sales_data = [
        {"customer_id": "a", "customer_name": "Ana", "total": 3000.0, "n_sales": 3},
        {"customer_id": "b", "customer_name": "Beto", "total": 10000.0, "n_sales": 2},
    ]

    with (
        patch(
            "app.persistence.repositories.customer_repository.CustomerRepository"
        ) as MockCR,
        patch(
            "app.persistence.repositories.transaction_repository.SaleRepository"
        ) as MockSR,
    ):
        cr_instance = AsyncMock()
        cr_instance.count_active = AsyncMock(return_value=2)
        MockCR.return_value = cr_instance

        sr_instance = AsyncMock()
        sr_instance.get_sales_by_customer = AsyncMock(return_value=sales_data)
        MockSR.return_value = sr_instance

        resp = await agent.process(_req(), task=_task("analizar_clientes"))

    assert resp.status == "success"
    assert resp.result["summary"] == "analizar_clientes"
    assert "Beto" in resp.message
    assert "Ana" in resp.message
    top = resp.result["structured_data"]["top"]
    assert top[0]["customer_name"] == "Beto"
    assert top[0]["total"] == 10000.0
    assert top[0]["n_sales"] == 2
    assert top[0]["avg_ticket"] == 5000.0  # 10000 / 2


# ── analizar_cuentas_por_cobrar con fiado → total adeudado correcto ──────────


async def test_analizar_cuentas_por_cobrar_total_correcto():
    """ANALYZE_SALES_DATA + analizar_cuentas_por_cobrar → total_owed = 2.000."""
    from app.application.agents.client.agent import AgentClient

    mock_db = MagicMock()
    agent = AgentClient(db=mock_db)

    receivables_data = [
        {"customer_id": "a", "customer_name": "Ana", "total_owed": 500.0, "n_sales": 1},
        {"customer_id": "b", "customer_name": "Beto", "total_owed": 1500.0, "n_sales": 2},
    ]

    with (
        patch(
            "app.persistence.repositories.customer_repository.CustomerRepository"
        ) as MockCR,
        patch(
            "app.persistence.repositories.transaction_repository.SaleRepository"
        ) as MockSR,
    ):
        cr_instance = AsyncMock()
        cr_instance.count_active = AsyncMock(return_value=2)
        MockCR.return_value = cr_instance

        sr_instance = AsyncMock()
        sr_instance.get_receivables_by_customer = AsyncMock(return_value=receivables_data)
        MockSR.return_value = sr_instance

        resp = await agent.process(_req(), task=_task("analizar_cuentas_por_cobrar"))

    assert resp.status == "success"
    assert resp.result["summary"] == "analizar_cuentas_por_cobrar"
    # Total adeudado: 500 + 1500 = 2.000 (determinístico — sin LLM)
    assert resp.result["structured_data"]["total_owed"] == 2000.0
    # Beto debe más → aparece en el mensaje
    assert "Beto" in resp.message
    # El mensaje debe mencionar el total
    assert "2.000" in resp.message or "2000" in resp.message
