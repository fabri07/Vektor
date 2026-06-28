"""Golden routing tests — consulta_libre EN español argentino.

Verifica que build_plan("consulta_libre", {"domain": d}) devuelva el agente
correcto de DOMAIN_TO_AGENT para cada uno de los 7 dominios canónicos.
NO testea el LLM del CEO — solo el ruteo determinístico de build_plan.
"""

import pytest

from app.application.agents.ceo.team_plan_builder import DOMAIN_TO_AGENT, build_plan
from app.application.agents.shared.schemas import ActionType


@pytest.mark.parametrize(
    "domain,expected_agent",
    [
        ("clientes", "agent_client"),
        ("ventas", "agent_income"),
        ("gastos", "agent_expense"),
        ("stock", "agent_stock"),
        ("proveedores", "agent_supplier"),
        ("caja", "agent_health"),
        ("marketing", "agent_marketing"),
    ],
)
def test_consulta_libre_routing_by_domain(domain: str, expected_agent: str) -> None:
    """build_plan rutea consulta_libre al agente correcto según domain."""
    plan = build_plan("consulta_libre", {"domain": domain})
    assert len(plan.tasks) == 1
    task = plan.tasks[0]
    assert task.agent == expected_agent, f"domain={domain!r}: esperado {expected_agent!r}, obtuvo {task.agent!r}"
    assert task.action_type == ActionType.ANSWER_DATA_QUERY
    assert task.entities["domain"] == domain
    assert plan.requires_synthesis is False
    assert plan.intent == "consulta_libre"


def test_consulta_libre_unknown_domain_fallback() -> None:
    """domain desconocido → agent_helper (fallback)."""
    plan = build_plan("consulta_libre", {"domain": "contabilidad"})
    assert plan.tasks[0].agent == "agent_helper"
    assert plan.tasks[0].action_type == ActionType.ANSWER_DATA_QUERY


def test_consulta_libre_missing_domain_fallback() -> None:
    """Sin domain en entities → agent_helper."""
    plan = build_plan("consulta_libre", {})
    assert plan.tasks[0].agent == "agent_helper"
    assert plan.tasks[0].action_type == ActionType.ANSWER_DATA_QUERY


def test_consulta_libre_none_domain_in_entities() -> None:
    """domain=None explícito → agent_helper."""
    plan = build_plan("consulta_libre", {"domain": None})
    assert plan.tasks[0].agent == "agent_helper"
    assert plan.tasks[0].action_type == ActionType.ANSWER_DATA_QUERY


def test_domain_to_agent_covers_exactly_7_domains() -> None:
    """DOMAIN_TO_AGENT cubre exactamente los 7 dominios canónicos."""
    expected = {"clientes", "ventas", "gastos", "stock", "proveedores", "caja", "marketing"}
    assert set(DOMAIN_TO_AGENT.keys()) == expected


def test_domain_to_agent_all_agents_valid() -> None:
    """Todos los agentes en DOMAIN_TO_AGENT son strings no vacíos."""
    for domain, agent in DOMAIN_TO_AGENT.items():
        assert isinstance(agent, str) and agent, f"Agente inválido para domain={domain!r}"


def test_consulta_libre_entities_preserved() -> None:
    """Entities extras se preservan en el plan junto a domain."""
    plan = build_plan("consulta_libre", {"domain": "ventas", "fecha": "ayer", "extra": 42})
    task = plan.tasks[0]
    assert task.entities["domain"] == "ventas"
    assert task.entities["fecha"] == "ayer"
    assert task.entities["extra"] == 42


def test_consulta_libre_generates_unique_plan_ids() -> None:
    """Cada build_plan genera plan_id y task_id distintos."""
    plan1 = build_plan("consulta_libre", {"domain": "ventas"})
    plan2 = build_plan("consulta_libre", {"domain": "ventas"})
    assert plan1.plan_id != plan2.plan_id
    assert plan1.tasks[0].task_id != plan2.tasks[0].task_id
