"""Catálogo de intents y constructor de AgentTeamPlan para AgentCEO.

Stage 1: solo emitía planes single-task.
Stage 3: genera planes compuestos para intents que requieren múltiples agentes.
"""

import uuid

from app.application.agents.shared.schemas import ActionType, AgentTask, AgentTeamPlan

# ── Keywords que indican compra a crédito (plazo) ─────────────────────────────
_CREDIT_KEYWORDS = frozenset(
    {"plazo", "credito", "crédito", "fiado", "dias", "días", "30 d", "60 d", "90 d", "a pagar"}
)

# ── Keys de entidades que indican un cobro simultáneo con la venta ────────────
_COBRO_ENTITY_KEYS = frozenset(
    {"cobrado", "medio_pago", "monto_cobrado", "forma_pago", "metodo_pago", "pago_efectivo"}
)

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


def _is_credit_purchase(entities: dict) -> bool:
    """Retorna True si las entidades indican una compra a crédito (no al contado)."""
    for val in entities.values():
        if isinstance(val, str):
            lowered = val.lower()
            if any(kw in lowered for kw in _CREDIT_KEYWORDS):
                return True
    return False


def _has_cobro_entity(entities: dict) -> bool:
    """Retorna True si las entidades incluyen datos de cobro simultáneo."""
    return bool(_COBRO_ENTITY_KEYS & set(entities.keys()))


def build_plan(intent: str, entities: dict) -> AgentTeamPlan:
    """Construye un AgentTeamPlan a partir del intent clasificado.

    Stage 3: genera planes compuestos (multi-task) para los siguientes intents:
    - importar_archivo_ventas    → (income, IMPORT_TABULAR_FILE) + (stock, UPDATE_STOCK) paralelo
    - registrar_compra_proveedor → (stock, REGISTER_PURCHASE) + (expense, REGISTER_CASH_OUTFLOW) si cash
    - ingresar_venta (con cobro) → (income, REGISTER_SALE) + (income, REGISTER_CASH_INFLOW) si cobro en entities

    Para el resto: plan de UNA sola tarea (compatible con Stage 1 y 2).
    """
    plan_id = str(uuid.uuid4())

    # ── importar_archivo_ventas → compound paralelo ───────────────────────────
    if intent == "importar_archivo_ventas":
        group_id = str(uuid.uuid4())
        task_income = AgentTask(
            task_id=str(uuid.uuid4()),
            agent="agent_income",
            action_type=ActionType.IMPORT_TABULAR_FILE,
            entities=entities,
            depends_on=[],
            approval_group=group_id,
        )
        task_stock = AgentTask(
            task_id=str(uuid.uuid4()),
            agent="agent_stock",
            action_type=ActionType.UPDATE_STOCK,
            entities=entities,
            depends_on=[],          # paralelo — sin dependencias entre sí
            approval_group=group_id,
        )
        return AgentTeamPlan(
            plan_id=plan_id,
            intent=intent,
            tasks=[task_income, task_stock],
            requires_synthesis=True,
        )

    # ── registrar_compra_proveedor cash → compound secuencial ─────────────────
    if intent == "registrar_compra_proveedor" and not _is_credit_purchase(entities):
        group_id = str(uuid.uuid4())
        task_stock = AgentTask(
            task_id=str(uuid.uuid4()),
            agent="agent_stock",
            action_type=ActionType.REGISTER_PURCHASE,
            entities=entities,
            depends_on=[],
            approval_group=group_id,
        )
        task_expense = AgentTask(
            task_id=str(uuid.uuid4()),
            agent="agent_expense",
            action_type=ActionType.REGISTER_CASH_OUTFLOW,
            entities=entities,
            depends_on=[task_stock.task_id],    # espera a que el stock se registre primero
            approval_group=group_id,
        )
        return AgentTeamPlan(
            plan_id=plan_id,
            intent=intent,
            tasks=[task_stock, task_expense],
            requires_synthesis=True,
        )

    # ── registrar_compra_proveedor crédito → single-task con advertencia ──────
    if intent == "registrar_compra_proveedor" and _is_credit_purchase(entities):
        task = AgentTask(
            task_id=str(uuid.uuid4()),
            agent="agent_stock",
            action_type=ActionType.REGISTER_PURCHASE,
            entities=entities,
        )
        return AgentTeamPlan(
            plan_id=plan_id,
            intent=intent,
            tasks=[task],
            requires_synthesis=False,
            fallback_message=(
                "Registré la compra en el inventario. "
                "Las cuentas por pagar (compras a crédito) aún no están soportadas — "
                "recordá registrar el pago cuando corresponda."
            ),
        )

    # ── ingresar_venta con cobro → compound secuencial ────────────────────────
    if intent == "ingresar_venta" and _has_cobro_entity(entities):
        group_id = str(uuid.uuid4())
        task_sale = AgentTask(
            task_id=str(uuid.uuid4()),
            agent="agent_income",
            action_type=ActionType.REGISTER_SALE,
            entities=entities,
            depends_on=[],
            approval_group=group_id,
        )
        task_cobro = AgentTask(
            task_id=str(uuid.uuid4()),
            agent="agent_income",
            action_type=ActionType.REGISTER_CASH_INFLOW,
            entities=entities,
            depends_on=[task_sale.task_id],
            approval_group=group_id,
        )
        return AgentTeamPlan(
            plan_id=plan_id,
            intent=intent,
            tasks=[task_sale, task_cobro],
            requires_synthesis=True,
        )

    # ── Caso general: single-task ─────────────────────────────────────────────
    action_type = INTENT_TO_ACTION_TYPE.get(intent, ActionType.ANSWER_HELP_REQUEST)
    agent = INTENT_TO_AGENT.get(intent, "agent_helper")

    task = AgentTask(
        task_id=str(uuid.uuid4()),
        agent=agent,
        action_type=action_type,
        entities=entities,
    )
    return AgentTeamPlan(
        plan_id=plan_id,
        intent=intent,
        tasks=[task],
        requires_synthesis=False,
    )
