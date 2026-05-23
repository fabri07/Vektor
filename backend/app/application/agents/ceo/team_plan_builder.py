"""Catálogo de intents y constructor de AgentTeamPlan para AgentCEO.

Stage 1: solo emite planes de una tarea (single-task).
Stage 3 extenderá build_plan() para generar planes multi-task y DAGs.
"""

import uuid

from app.application.agents.shared.schemas import ActionType, AgentTask, AgentTeamPlan

# ── Catálogo cerrado de intents (17, español rioplatense) ─────────────────────
INTENT_CATALOG: list[str] = [
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
    "generar_informe",
    "gestionar_proveedor",
    "sincronizar_google",
    "agendar_evento",
    "ayuda_plataforma",
    "intent_desconocido",
]

# ── Intent → agente especializado ─────────────────────────────────────────────
# Stage 2: los intents nuevos rutean a agent_income/agent_expense/agent_google.
# Los aliases legacy se conservan en registry.py para PendingActions históricas.
INTENT_TO_AGENT: dict[str, str] = {
    "ingresar_venta":             "agent_income",
    "ingresar_cobro":             "agent_income",
    "ingresar_gasto":             "agent_expense",
    "ingresar_pago_salida":       "agent_expense",
    "actualizar_stock":           "agent_stock",
    "registrar_merma":            "agent_stock",
    "actualizar_producto":        "agent_stock",
    "importar_archivo_ventas":    "agent_income",   # Stage 3: compound + (agent_stock, UPDATE_STOCK)
    "importar_archivo_gastos":    "agent_expense",
    # Stage 3: compound → (agent_stock, REGISTER_PURCHASE) + (agent_expense, REGISTER_CASH_OUTFLOW)
    "registrar_compra_proveedor": "agent_supplier",
    "consultar_estado_negocio":   "agent_health",
    "generar_informe":            "agent_health",
    "gestionar_proveedor":        "agent_supplier",
    "sincronizar_google":         "agent_google",
    "agendar_evento":             "agent_google",
    "ayuda_plataforma":           "agent_helper",
    "intent_desconocido":         "agent_helper",
}

# ── Intent → ActionType (catálogo cerrado) ────────────────────────────────────
INTENT_TO_ACTION_TYPE: dict[str, ActionType] = {
    "ingresar_venta":             ActionType.REGISTER_SALE,
    "ingresar_cobro":             ActionType.REGISTER_CASH_INFLOW,
    "ingresar_gasto":             ActionType.REGISTER_EXPENSE,
    "ingresar_pago_salida":       ActionType.REGISTER_CASH_OUTFLOW,
    "actualizar_stock":           ActionType.UPDATE_STOCK,
    "registrar_merma":            ActionType.REGISTER_STOCK_LOSS,
    "actualizar_producto":        ActionType.UPDATE_PRODUCT,
    "importar_archivo_ventas":    ActionType.IMPORT_TABULAR_FILE,
    "importar_archivo_gastos":    ActionType.IMPORT_TABULAR_FILE,
    "registrar_compra_proveedor": ActionType.REGISTER_PURCHASE,
    "consultar_estado_negocio":   ActionType.GENERATE_HEALTH_REPORT,
    "generar_informe":            ActionType.GENERATE_HEALTH_REPORT,
    "gestionar_proveedor":        ActionType.CREATE_SUPPLIER_DRAFT,
    "sincronizar_google":         ActionType.SYNC_TO_GOOGLE,
    "agendar_evento":             ActionType.CREATE_CALENDAR_EVENT,
    "ayuda_plataforma":           ActionType.ANSWER_HELP_REQUEST,
    "intent_desconocido":         ActionType.ANSWER_HELP_REQUEST,
}


def build_plan(intent: str, entities: dict) -> AgentTeamPlan:
    """Construye un AgentTeamPlan a partir del intent clasificado.

    Stage 1: siempre genera un plan de UNA sola tarea.
    Stage 3 extenderá esta función para generar planes multi-task y DAGs.

    Intents marcados como deuda multi-task (NO multi-task en Stage 1):
    - registrar_compra_proveedor → debería ser (stock, REGISTER_PURCHASE) + (expense, REGISTER_CASH_OUTFLOW)
    - ingresar_venta_con_cobro   → debería ser (income, REGISTER_SALE) + (income, REGISTER_CASH_INFLOW)
    - importar_archivo_ventas    → debería ser (income, IMPORT) + (stock, UPDATE_STOCK)

    El guard contra multi-task está en el ChatOrchestrator: si por algún bug esta función
    retorna len(tasks) > 1, el orchestrator lo detecta y retorna error explícito.
    """
    action_type = INTENT_TO_ACTION_TYPE.get(intent, ActionType.ANSWER_HELP_REQUEST)
    agent = INTENT_TO_AGENT.get(intent, "agent_helper")

    task = AgentTask(
        task_id=str(uuid.uuid4()),
        agent=agent,
        action_type=action_type,
        entities=entities,
    )

    return AgentTeamPlan(
        plan_id=str(uuid.uuid4()),
        intent=intent,
        tasks=[task],
        requires_synthesis=False,
    )
