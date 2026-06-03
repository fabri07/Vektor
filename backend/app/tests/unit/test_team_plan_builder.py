"""Tests para team_plan_builder — contrato del catálogo de intents y construcción de planes."""

from app.application.agents.ceo.team_plan_builder import (
    INTENT_CATALOG,
    INTENT_TO_ACTION_TYPE,
    INTENT_TO_AGENT,
    build_plan,
)
from app.application.agents.shared.schemas import ActionType, AgentTeamPlan

# ── Catálogo ──────────────────────────────────────────────────────────────────


def test_intent_catalog_size():
    # 18 previos + 42 analíticos/fallback Sprint 17 = 60
    assert len(INTENT_CATALOG) == 60


def test_all_intents_have_agent_mapping():
    """Todo intent del catálogo tiene un agente destino."""
    for intent in INTENT_CATALOG:
        assert intent in INTENT_TO_AGENT, f"Intent sin agente: {intent}"


def test_all_intents_have_action_type_mapping():
    """Todo intent del catálogo tiene un ActionType."""
    for intent in INTENT_CATALOG:
        assert intent in INTENT_TO_ACTION_TYPE, f"Intent sin ActionType: {intent}"


def test_no_extra_mappings_vs_catalog():
    """No hay mappings huérfanos que no estén en INTENT_CATALOG."""
    catalog_set = set(INTENT_CATALOG)
    assert set(INTENT_TO_AGENT.keys()) == catalog_set
    assert set(INTENT_TO_ACTION_TYPE.keys()) == catalog_set


# ── build_plan — correctness ──────────────────────────────────────────────────


def test_build_plan_ingresar_venta():
    plan = build_plan("ingresar_venta", {"monto": 1500})
    assert isinstance(plan, AgentTeamPlan)
    assert len(plan.tasks) == 1
    assert plan.tasks[0].action_type == ActionType.REGISTER_SALE
    assert plan.tasks[0].agent == "agent_income"
    assert plan.tasks[0].entities["monto"] == 1500
    assert plan.intent == "ingresar_venta"
    assert plan.requires_synthesis is False


def test_build_plan_ingresar_gasto():
    plan = build_plan("ingresar_gasto", {"descripcion": "alquiler"})
    assert plan.tasks[0].action_type == ActionType.REGISTER_EXPENSE
    assert plan.tasks[0].agent == "agent_expense"


def test_build_plan_registrar_merma():
    plan = build_plan("registrar_merma", {})
    assert plan.tasks[0].action_type == ActionType.REGISTER_STOCK_LOSS
    assert plan.tasks[0].agent == "agent_stock"


def test_build_plan_consultar_estado():
    plan = build_plan("consultar_estado_negocio", {})
    assert plan.tasks[0].action_type == ActionType.GENERATE_HEALTH_REPORT
    assert plan.tasks[0].agent == "agent_health"


def test_build_plan_ayuda_plataforma():
    plan = build_plan("ayuda_plataforma", {})
    assert plan.tasks[0].action_type == ActionType.ANSWER_HELP_REQUEST
    assert plan.tasks[0].agent == "agent_helper"


def test_build_plan_intent_desconocido_fallback():
    plan = build_plan("intent_desconocido", {})
    assert plan.tasks[0].action_type == ActionType.ANSWER_HELP_REQUEST
    assert plan.tasks[0].agent == "agent_helper"


def test_build_plan_unknown_intent_fallback():
    """Intent no registrado cae en agent_helper / ANSWER_HELP_REQUEST."""
    plan = build_plan("esto_no_existe", {})
    assert plan.tasks[0].action_type == ActionType.ANSWER_HELP_REQUEST
    assert plan.tasks[0].agent == "agent_helper"


# ── build_plan — invariantes estructurales ────────────────────────────────────

# Intents que generan planes compuestos (> 1 tarea)
# Stage 3: importar_archivo_ventas, registrar_compra_proveedor
# Stage 4: generar_informe_con_export
_COMPOUND_INTENTS = {
    "importar_archivo_ventas",
    "registrar_compra_proveedor",
    "generar_informe_con_export",
}


def test_build_plan_single_task_for_non_compound_intents():
    """Stage 3: los intents que no son compuestos retornan exactamente 1 tarea."""
    single_task_intents = [i for i in INTENT_CATALOG if i not in _COMPOUND_INTENTS]
    for intent in single_task_intents:
        plan = build_plan(intent, {})
        assert len(plan.tasks) == 1, f"build_plan({intent!r}) retornó {len(plan.tasks)} tasks"


def test_build_plan_compound_importar_ventas():
    """Stage 3: importar_archivo_ventas genera 2 tareas paralelas."""
    plan = build_plan("importar_archivo_ventas", {})
    assert len(plan.tasks) == 2
    agents = {t.agent for t in plan.tasks}
    assert "agent_income" in agents
    assert "agent_stock" in agents
    assert plan.requires_synthesis is True
    # Todas en el mismo approval_group, sin dependencias (paralelo)
    groups = {t.approval_group for t in plan.tasks}
    assert len(groups) == 1
    for task in plan.tasks:
        assert task.depends_on == []


def test_build_plan_compound_compra_proveedor_cash():
    """Stage 3: compra al contado genera 2 tareas secuenciales."""
    plan = build_plan("registrar_compra_proveedor", {"monto": 5000})
    assert len(plan.tasks) == 2
    stock_task = next(t for t in plan.tasks if t.agent == "agent_stock")
    expense_task = next(t for t in plan.tasks if t.agent == "agent_expense")
    assert expense_task.depends_on == [stock_task.task_id]
    assert plan.requires_synthesis is True


def test_build_plan_compound_compra_proveedor_credito():
    """Stage 3: compra a crédito genera 1 tarea (no hay outflow inmediato)."""
    plan = build_plan("registrar_compra_proveedor", {"forma_pago": "a 30 dias"})
    assert len(plan.tasks) == 1
    assert plan.requires_synthesis is False
    assert plan.fallback_message is not None


def test_build_plan_venta_con_cobro():
    """Stage 3: ingresar_venta con entidad cobro → 2 tareas secuenciales."""
    plan = build_plan("ingresar_venta", {"monto": 2000, "medio_pago": "efectivo"})
    assert len(plan.tasks) == 2
    assert plan.requires_synthesis is True


def test_build_plan_generates_unique_ids():
    """Cada llamada a build_plan genera plan_id y task_id distintos."""
    plan1 = build_plan("ingresar_venta", {})
    plan2 = build_plan("ingresar_venta", {})
    assert plan1.plan_id != plan2.plan_id
    assert plan1.tasks[0].task_id != plan2.tasks[0].task_id


def test_build_plan_entities_isolated():
    """Las entities no son un mutable compartido entre instancias."""
    entities = {"monto": 100}
    plan = build_plan("ingresar_venta", entities)
    plan.tasks[0].entities["extra"] = "x"
    # El dict original no debe mutarse
    assert "extra" not in entities


# ── Stage 4: generar_informe_con_export ───────────────────────────────────────


def test_build_plan_generar_informe_con_export_dag():
    """Stage 4: informe + upload a Drive genera DAG de 2 tareas con dependencia."""
    plan = build_plan("generar_informe_con_export", {})
    assert len(plan.tasks) == 2
    health_task = next(t for t in plan.tasks if t.agent == "agent_health")
    upload_task = next(t for t in plan.tasks if t.agent == "agent_google")
    assert health_task.action_type == ActionType.GENERATE_HEALTH_REPORT
    assert upload_task.action_type == ActionType.UPLOAD_TO_DRIVE
    # DAG: upload depende del health report
    assert upload_task.depends_on == [health_task.task_id]
    # Sin synthesizer — el health report habla por sí mismo
    assert plan.requires_synthesis is False


def test_build_plan_new_action_types_in_risk_engine():
    """Los 3 ActionTypes nuevos del Stage 4 están en el RiskEngine."""
    from app.application.agents.shared.risk_engine import RiskEngine

    assert RiskEngine.evaluate(ActionType.UPLOAD_TO_DRIVE) is not None
    assert RiskEngine.evaluate(ActionType.CREATE_GOOGLE_DOC) is not None
    assert RiskEngine.evaluate(ActionType.APPEND_TO_SHEET) is not None


def test_tool_broker_importable():
    """GoogleToolBroker se puede importar desde el módulo correcto."""
    from app.application.agents.google.tool_broker import GoogleToolBroker

    assert GoogleToolBroker is not None
