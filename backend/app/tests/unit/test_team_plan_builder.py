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
    # Sprint 19: catálogo consolidado. 11 escritura + 2 informes + 3 google +
    # 1 ayuda + 1 desconocido + 8 familias analíticas + 2 sentinels aclaración = 28.
    # Nivel 2: + reclasificar_gasto = 29.
    # v4 Fase 2: + analizar_clientes = 30.
    # v4 Fase 4: + analizar_marketing = 31.
    # Las 35 variantes analíticas viejas ahora son valores de `analysis_type`.
    assert len(INTENT_CATALOG) == 31


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


def test_build_plan_reclasificar_gasto():
    plan = build_plan("reclasificar_gasto", {"target": "reventa"})
    assert len(plan.tasks) == 1
    assert plan.tasks[0].action_type == ActionType.RECLASSIFY_EXPENSE
    assert plan.tasks[0].agent == "agent_expense"
    assert plan.tasks[0].entities["target"] == "reventa"
    assert plan.requires_synthesis is False


def test_reclassify_expense_in_risk_engine_is_medium():
    from app.application.agents.shared.risk_engine import RiskEngine
    from app.application.agents.shared.schemas import RiskLevel

    assert RiskEngine.evaluate(ActionType.RECLASSIFY_EXPENSE) == RiskLevel.MEDIUM
    assert RiskEngine.requires_approval(ActionType.RECLASSIFY_EXPENSE) is True


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


# ── Sprint 19: resolver de analysis_type → _intent legacy ─────────────────────


def test_analytic_intent_default_discriminator():
    """Sin analysis_type, el intent analítico inyecta el _intent default de la familia."""
    plan = build_plan("analizar_precios", {})
    assert plan.tasks[0].action_type == ActionType.ANALYZE_PRICES
    assert plan.tasks[0].agent == "agent_stock"
    assert plan.tasks[0].entities["_intent"] == "analizar_margenes_productos"


def test_analytic_intent_explicit_analysis_type():
    """analysis_type válido se traduce al _intent legacy correcto."""
    plan = build_plan("analizar_precios", {"analysis_type": "simulacion"})
    assert plan.tasks[0].entities["_intent"] == "simular_actualizacion_precios"


def test_analytic_intent_invalid_analysis_type_falls_to_default():
    """analysis_type desconocido cae al default de la familia (no rompe)."""
    plan = build_plan("analizar_stock", {"analysis_type": "no_existe"})
    assert plan.tasks[0].entities["_intent"] == "analizar_stock"


def test_analytic_families_cover_each_action_type():
    """Cada familia analítica resuelve sus analysis_type a _intents legacy distintos."""
    cases = {
        "analizar_stock": ("quiebres", "detectar_quiebres_stock"),
        "analizar_ventas": ("estrella", "detectar_productos_estrella"),
        "analizar_gastos": ("punto_equilibrio", "calcular_punto_equilibrio"),
        "analizar_proveedores": ("dependencia", "detectar_dependencia_proveedor"),
        "proyectar_caja": ("what_if", "simular_escenario_financiero"),
        "analizar_archivo": ("tipo", "detectar_tipo_archivo"),
        "analizar_clientes": ("inactivos", "detectar_clientes_inactivos"),
    }
    for intent, (analysis_type, expected) in cases.items():
        plan = build_plan(intent, {"analysis_type": analysis_type})
        assert plan.tasks[0].entities["_intent"] == expected, intent


def test_legacy_alias_still_routes_and_preserves_intent():
    """Un key granular viejo (en vuelo) rutea al agente correcto e inyecta _intent tal cual."""
    plan = build_plan("detectar_quiebres_stock", {})
    assert plan.tasks[0].action_type == ActionType.ANALYZE_STOCK_DATA
    assert plan.tasks[0].agent == "agent_stock"
    assert plan.tasks[0].entities["_intent"] == "detectar_quiebres_stock"


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


# ── v4 Fase 4: analizar_marketing ────────────────────────────────────────────


def test_analizar_marketing_in_intent_catalog():
    """analizar_marketing está en INTENT_CATALOG con sus triggers."""
    assert "analizar_marketing" in INTENT_CATALOG
    assert len(INTENT_CATALOG["analizar_marketing"]["triggers"]) > 0


def test_analizar_marketing_to_agent_mapping():
    """analizar_marketing rutea a agent_marketing."""
    assert INTENT_TO_AGENT["analizar_marketing"] == "agent_marketing"


def test_analizar_marketing_to_action_type_mapping():
    """analizar_marketing usa ANALYZE_MARKETING_DATA."""
    assert INTENT_TO_ACTION_TYPE["analizar_marketing"] == ActionType.ANALYZE_MARKETING_DATA


def test_analyze_marketing_data_in_risk_engine_low():
    """ANALYZE_MARKETING_DATA está en RiskEngine como LOW, sin aprobación."""
    from app.application.agents.shared.risk_engine import ACTION_RISK_MAP, RiskEngine
    from app.application.agents.shared.schemas import RiskLevel

    assert ACTION_RISK_MAP[ActionType.ANALYZE_MARKETING_DATA] == RiskLevel.LOW
    assert RiskEngine.evaluate(ActionType.ANALYZE_MARKETING_DATA) == RiskLevel.LOW
    assert RiskEngine.requires_approval(ActionType.ANALYZE_MARKETING_DATA) is False


def test_analyze_marketing_data_in_intent_aware_action_types():
    """ANALYZE_MARKETING_DATA está en _INTENT_AWARE_ACTION_TYPES."""
    from app.application.agents.ceo.team_plan_builder import _INTENT_AWARE_ACTION_TYPES

    assert ActionType.ANALYZE_MARKETING_DATA in _INTENT_AWARE_ACTION_TYPES


def test_build_plan_analizar_marketing_default():
    """build_plan('analizar_marketing') genera 1 tarea con agent_marketing."""
    plan = build_plan("analizar_marketing", {})
    assert len(plan.tasks) == 1
    assert plan.tasks[0].agent == "agent_marketing"
    assert plan.tasks[0].action_type == ActionType.ANALYZE_MARKETING_DATA
    assert plan.tasks[0].entities["_intent"] == "analizar_marketing"


def test_build_plan_analizar_marketing_roi_ads():
    """analizar_marketing con analysis_type='roi_ads' → _intent='analizar_roi_ads'."""
    plan = build_plan("analizar_marketing", {"analysis_type": "roi_ads"})
    assert plan.tasks[0].entities["_intent"] == "analizar_roi_ads"


def test_build_plan_analizar_marketing_sugerir_campana():
    """analizar_marketing con analysis_type='sugerir_campana' → _intent='sugerir_campana'."""
    plan = build_plan("analizar_marketing", {"analysis_type": "sugerir_campana"})
    assert plan.tasks[0].entities["_intent"] == "sugerir_campana"
