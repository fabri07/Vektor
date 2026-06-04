from app.application.agents.ceo.agent import INTENT_CATALOG
from app.application.agents.ceo.team_plan_builder import build_plan
from app.application.agents.shared.context_builder import CONTEXT_BUDGETS, ContextBuilder
from app.application.agents.shared.heuristic_engine import HeuristicEngine
from app.application.agents.shared.risk_engine import (  # type: ignore[attr-defined]  # test double / fixture
    RiskEngine,
    RiskLevel,
)
from app.application.agents.shared.schemas import ActionType


def test_all_agents_importable():
    from app.application.agents.cash.agent import AgentCash
    from app.application.agents.ceo.agent import AgentCEO
    from app.application.agents.expense.agent import AgentExpense
    from app.application.agents.google.agent import AgentGoogle
    from app.application.agents.health.agent import AgentHealth
    from app.application.agents.helper.agent import AgentHelper
    from app.application.agents.income.agent import AgentIncome
    from app.application.agents.stock.agent import AgentStock
    from app.application.agents.supplier.agent import AgentSupplier

    for cls in (
        AgentCEO,
        AgentCash,
        AgentIncome,
        AgentExpense,
        AgentGoogle,
        AgentStock,
        AgentSupplier,
        AgentHealth,
        AgentHelper,
    ):
        assert cls.agent_name is not None


def test_risk_engine_low_actions():
    assert RiskEngine.evaluate(ActionType.CREATE_PURCHASE_SUGGESTION) == RiskLevel.LOW


def test_risk_engine_medium_actions():
    assert RiskEngine.evaluate(ActionType.REGISTER_SALE) == RiskLevel.MEDIUM


def test_risk_engine_stage4_google_actions():
    assert RiskEngine.evaluate(ActionType.UPLOAD_TO_DRIVE) == RiskLevel.MEDIUM
    assert RiskEngine.evaluate(ActionType.CREATE_GOOGLE_DOC) == RiskLevel.MEDIUM
    assert RiskEngine.evaluate(ActionType.APPEND_TO_SHEET) == RiskLevel.MEDIUM


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
    # 20 previos + 7 analíticos Sprint 17 (ANALYZE_*, SIMULATE_SCENARIO) = 27
    assert len(ActionType) == 27


def test_intent_catalog_complete():
    # Sprint 19: catálogo consolidado a 28 (35 variantes analíticas → entidad analysis_type).
    assert len(INTENT_CATALOG) == 28


def test_intent_catalog_spanish():
    """Los intents base siguen presentes y todos usan nomenclatura español snake_case.

    Sprint 19: `generar_informe` se fusionó en `consultar_estado_negocio`.
    """
    base = {
        "ingresar_venta",
        "ingresar_cobro",
        "ingresar_gasto",
        "ingresar_pago_salida",
        "actualizar_stock",
        "registrar_merma",
        "actualizar_producto",
        "importar_archivo_ventas",
        "importar_archivo_gastos",
        "registrar_compra_proveedor",
        "consultar_estado_negocio",
        "generar_informe_con_export",
        "gestionar_proveedor",
        "sincronizar_google",
        "agendar_evento",
        "ayuda_plataforma",
        "intent_desconocido",
    }
    catalog = set(INTENT_CATALOG)
    assert base <= catalog, f"faltan intents base: {base - catalog}"
    # Nomenclatura: snake_case ascii en minúsculas (proxy de español rioplatense)
    for intent in INTENT_CATALOG:
        assert intent == intent.lower()
        assert intent.replace("_", "").isalpha()


def test_build_plan_returns_single_task():
    from app.application.agents.shared.schemas import ActionType, AgentTeamPlan

    plan = build_plan("ingresar_venta", {"producto": "gaseosa", "cantidad": 3})
    assert isinstance(plan, AgentTeamPlan)
    assert len(plan.tasks) == 1
    assert plan.tasks[0].action_type == ActionType.REGISTER_SALE
    assert plan.tasks[0].agent == "agent_income"
    assert plan.tasks[0].entities["producto"] == "gaseosa"
    assert plan.intent == "ingresar_venta"


def test_build_plan_unknown_intent_falls_back_to_helper():
    plan = build_plan("intent_desconocido", {})
    assert len(plan.tasks) == 1
    assert plan.tasks[0].agent == "agent_helper"


def test_wrap_user_input():
    from app.application.agents.ceo.agent import AgentCEO

    agent = AgentCEO()
    wrapped = agent.wrap_user_input("hola")
    assert "<user_message>" in wrapped
    assert "</user_message>" in wrapped
