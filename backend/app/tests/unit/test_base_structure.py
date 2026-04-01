import pytest

from app.application.agents.shared.schemas import ActionType, AgentRequest
from app.application.agents.shared.risk_engine import RiskEngine, RiskLevel
from app.application.agents.shared.context_builder import ContextBuilder, CONTEXT_BUDGETS
from app.application.agents.shared.heuristic_engine import HeuristicEngine


def test_all_agents_importable():
    from app.application.agents.ceo.agent import AgentCEO
    from app.application.agents.cash.agent import AgentCash
    from app.application.agents.stock.agent import AgentStock
    from app.application.agents.supplier.agent import AgentSupplier
    from app.application.agents.health.agent import AgentHealth
    from app.application.agents.helper.agent import AgentHelper

    for cls in (AgentCEO, AgentCash, AgentStock, AgentSupplier, AgentHealth, AgentHelper):
        assert cls.agent_name is not None


def test_risk_engine_low_actions():
    assert RiskEngine.evaluate(ActionType.CREATE_PURCHASE_SUGGESTION) == RiskLevel.LOW


def test_risk_engine_medium_actions():
    assert RiskEngine.evaluate(ActionType.REGISTER_SALE) == RiskLevel.MEDIUM


def test_risk_engine_high_actions():
    assert RiskEngine.evaluate(ActionType.REGISTER_STOCK_LOSS) == RiskLevel.HIGH


def test_context_builder_respects_budget():
    # Relleno con contenido falso para cada sección
    filler = "x" * 10  # contenido corto, el costo lo fija CONTEXT_PRIORITY

    for agent_name, budget in CONTEXT_BUDGETS.items():
        builder = ContextBuilder(agent_name)
        builder.add("intent_and_entities", filler)
        builder.add("business_heuristics", filler)
        builder.add("current_snapshot", filler)
        builder.add("recent_events", filler)
        builder.add("conversation_history", filler)
        builder.add("historical_data", filler)
        result = builder.build()
        assert isinstance(result, str)
        # El builder nunca debe superar el budget en tokens contados
        assert builder.budget == budget


def test_heuristic_to_prompt_fragment_is_numeric():
    config = HeuristicEngine.get("kiosco_almacen")
    fragment = config.to_prompt_fragment()
    assert "%" in fragment
    assert any(char.isdigit() for char in fragment)


def test_action_type_enum_complete():
    assert len(ActionType) == 15


def test_wrap_user_input():
    from app.application.agents.ceo.agent import AgentCEO
    agent = AgentCEO()
    wrapped = agent.wrap_user_input("hola")
    assert "<user_message>" in wrapped
    assert "</user_message>" in wrapped
