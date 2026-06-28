"""Catálogo de intents y constructor de AgentTeamPlan para AgentCEO.

Stage 1: solo emitía planes single-task.
Stage 3: genera planes compuestos para intents que requieren múltiples agentes.
Sprint 19: catálogo consolidado. Las 8 familias analíticas (35 intents granulares)
colapsan a 8 intents; la granularidad se mueve a la entidad `analysis_type`. El
clasificador elige entre ~25 intents (no ~60); el sub-análisis lo resuelve
`_resolve_legacy_discriminator()` traduciendo `analysis_type` → el `_intent` legacy
que los handlers ya saben interpretar. Los handlers NO cambian.
"""

import uuid
from typing import Any

from app.application.agents.shared.schemas import ActionType, AgentTask, AgentTeamPlan

# ── Keywords que indican compra a crédito (plazo) ─────────────────────────────
_CREDIT_KEYWORDS = frozenset(
    {"plazo", "credito", "crédito", "fiado", "dias", "días", "30 d", "60 d", "90 d", "a pagar"}
)

# ── Keys de entidades que indican un cobro simultáneo con la venta ────────────
_COBRO_ENTITY_KEYS = frozenset(
    {"cobrado", "medio_pago", "monto_cobrado", "forma_pago", "metodo_pago", "pago_efectivo"}
)

# ── Catálogo cerrado de intents con descripción + disparadores en rioplatense ──
# Estructura: {intent_key: {"desc": str, "triggers": list[str]}}
# - desc: qué acción representa (usada en logs y en el prompt del CEO)
# - triggers: ejemplos reales de cómo lo expresa un usuario argentino
#   (el CEO los incluye en su system prompt para mejor clasificación)
#
# Sprint 19: los intents analíticos son UNO por familia. El sub-análisis específico
# va en la entidad `analysis_type` (ver _ANALYTIC_FAMILIES). Para agregar un intent
# nuevo: (1) agregarlo aquí, (2) en INTENT_TO_AGENT, (3) en INTENT_TO_ACTION_TYPE,
# (4) en RiskEngine si usa un ActionType nuevo. Para agregar un sub-análisis a una
# familia existente: agregarlo solo en _ANALYTIC_FAMILIES (no toca el clasificador).
INTENT_CATALOG: dict[str, dict[str, object]] = {
    # ── Operaciones de negocio (escribe datos) ────────────────────────────────
    "ingresar_venta": {
        "desc": "Registrar una venta realizada",
        "triggers": [
            "vendí 3 jabones a $500",
            "hice una venta hoy",
            "anotame una venta de $1200",
            "me compraron 2 packs de yerba",
            "sale de medialunas $400",
            "entró plata por venta",
        ],
    },
    "ingresar_cobro": {
        "desc": "Registrar cobro de una deuda o ingreso extraordinario",
        "triggers": [
            "cobré lo que me debía Juan",
            "me pagaron la deuda de $3000",
            "ingresó plata de un cobro",
            "me saldaron la deuda",
            "llegó el pago atrasado",
        ],
    },
    "ingresar_gasto": {
        "desc": "Registrar un gasto operativo",
        "triggers": [
            "pagué el alquiler $15000",
            "gasté en luz y gas",
            "aboné el servicio de internet",
            "pagué el sueldo del empleado",
            "compré útiles de oficina",
            "pagué la monotributo",
        ],
    },
    "ingresar_pago_salida": {
        "desc": "Registrar un pago de deuda o salida de caja",
        "triggers": [
            "pagué una deuda al proveedor",
            "transferí $5000 a la distribuidora",
            "salida de caja por pago de deuda",
            "liquidé una deuda pendiente",
        ],
    },
    "actualizar_stock": {
        "desc": "Ajustar stock de un producto",
        "triggers": [
            "tengo 50 unidades de Coca Cola",
            "ajustá el stock de jabón a 30 unidades",
            "corregí el inventario de yerba",
            "el conteo me dio diferente",
        ],
    },
    "registrar_merma": {
        "desc": "Registrar pérdida, rotura o vencimiento de producto",
        "triggers": [
            "se vencieron 5 yogures",
            "se rompió una botella de aceite",
            "merma de 3 unidades de leche",
            "se dañó mercadería",
            "caducaron productos",
        ],
    },
    "actualizar_producto": {
        "desc": "Modificar precio, costo, umbral u otro dato de un producto",
        "triggers": [
            "cambiá el precio del pan a $350",
            "actualizá el costo de la yerba",
            "subí el precio de todas las gaseosas un 12%",
            "renombrá el producto Coca 500ml",
            "desactivá el producto que ya no vendo",
        ],
    },
    "importar_archivo_ventas": {
        "desc": "Importar ventas desde un archivo Excel/CSV",
        "triggers": [
            "subí un Excel con las ventas del mes",
            "adjunté el CSV de ventas",
            "cargar las ventas desde la planilla",
        ],
    },
    "importar_archivo_gastos": {
        "desc": "Importar gastos desde un archivo Excel/CSV",
        "triggers": [
            "subí la planilla de gastos",
            "importar egresos del mes desde archivo",
            "adjunté el Excel de gastos",
        ],
    },
    "importar_archivo_productos": {
        "desc": "Importar catálogo de productos desde archivo",
        "triggers": [
            "subí mi lista de precios para cargar",
            "cargar catálogo desde archivo",
            "importar mis productos del Excel",
        ],
    },
    "registrar_compra_proveedor": {
        "desc": "Registrar compra de mercadería a proveedor",
        "triggers": [
            "compré mercadería a Mayorista Norte",
            "llegó el pedido de la distribuidora",
            "recibí merca de mi proveedor",
            "compré stock por $20000",
        ],
    },
    "reclasificar_gasto": {
        "desc": "Reclasificar un gasto entre mercadería de reventa, insumo u otra categoría "
        "(muta su clasificación contable)",
        "triggers": [
            "reclasificá ese gasto",
            "recategorizá esta compra",
            "esto es mercadería o insumo",
            "cambialo a mercadería",
            "esto va como gasto",
            "esto es un insumo, no reventa",
            "marcá esto como reventa",
            "este gasto en realidad es mercadería",
            "movelo a insumos",
        ],
    },
    # ── Informes y estado ─────────────────────────────────────────────────────
    "consultar_estado_negocio": {
        "desc": "Ver el estado de salud financiera del negocio o generar un informe",
        "triggers": [
            "cómo está mi negocio",
            "score de salud financiera",
            "dame un resumen del negocio",
            "generá un informe completo",
            "reporte del mes",
            "dame un análisis integral",
        ],
    },
    "generar_informe_con_export": {
        "desc": "Generar informe y subirlo a Google Drive",
        "triggers": [
            "generá el informe y subilo a Drive",
            "exportá el reporte a Google Drive",
            "mandame el informe a Drive",
        ],
    },
    # ── Google y acciones externas ────────────────────────────────────────────
    "gestionar_proveedor": {
        "desc": "Redactar o enviar email a proveedor via Gmail",
        "triggers": [
            "mandá un mail al proveedor",
            "armá un email a Mayorista Norte",
            "redactá un mensaje para el proveedor",
        ],
    },
    "sincronizar_google": {
        "desc": "Sincronizar datos con Google Sheets o Drive",
        "triggers": [
            "sincronizá las ventas con Sheets",
            "exportar datos a Drive",
            "subí los datos a la planilla",
        ],
    },
    "agendar_evento": {
        "desc": "Crear evento en Google Calendar",
        "triggers": [
            "agendá una reunión el martes a las 10",
            "crear evento en Google Calendar",
            "recordatorio para el pago del alquiler",
            "anotame en la agenda",
        ],
    },
    # ── Ayuda y fallback ──────────────────────────────────────────────────────
    "ayuda_plataforma": {
        "desc": "Preguntas sobre cómo usar Véktor o qué se puede hacer con los datos/archivos",
        "triggers": [
            "cómo uso Véktor",
            "qué puedo hacer con la plataforma",
            "tengo una duda sobre la app",
            "qué puedo hacer con este archivo",
            "qué podés hacer con estos datos",
        ],
    },
    "intent_desconocido": {
        "desc": "Mensaje fuera del scope de Véktor o no clasificable",
        "triggers": [],
    },
    # ── Familias analíticas consolidadas (read-only) ──────────────────────────
    # Cada una cubre varios sub-análisis vía la entidad `analysis_type`.
    "analizar_archivo": {
        "desc": "Analizar un archivo adjunto: contenido, tipo, limpieza o resumen",
        "triggers": [
            "mirá este archivo que subí",
            "qué hay en esta planilla",
            "qué tipo de archivo es este",
            "limpiá esta planilla",
            "resumime este archivo",
            "dame un diagnóstico del archivo",
        ],
    },
    "analizar_precios": {
        "desc": "Analizar precios/márgenes: márgenes, sugerencias, comparación, "
        "aumentos de proveedor, simulación o sensibilidad",
        "triggers": [
            "cuánto gano por producto",
            "a qué precio vendo esto para ganar bien",
            "compará la lista vieja con la nueva",
            "cuánto me aumentó la distribuidora",
            "simulá un aumento del 10%",
            "analizá esta lista de precios",
        ],
    },
    "analizar_stock": {
        "desc": "Analizar inventario: panorama, quiebres, sobrestock, reposición o días de stock",
        "triggers": [
            "cómo está mi stock",
            "qué me falta reponer",
            "qué tengo de más",
            "qué compro primero",
            "para cuántos días me alcanza el stock",
        ],
    },
    "analizar_ventas": {
        "desc": "Analizar ventas: rentabilidad, productos estrella/problemáticos, "
        "ticket o descuentos",
        "triggers": [
            "qué productos me dejan más plata en ventas",
            "cuáles son mis productos estrella",
            "qué productos vendo mucho pero me dejan poco",
            "cuál es mi ticket promedio",
            "los descuentos me están comiendo el margen",
        ],
    },
    "analizar_gastos": {
        "desc": "Analizar gastos: clasificación, recurrentes, anómalos, costos fijos/variables "
        "o punto de equilibrio",
        "triggers": [
            "cómo están distribuidos mis gastos",
            "qué gastos fijos tengo",
            "hay algún gasto raro o inusual",
            "separame costos fijos de variables",
            "cuánto tengo que vender para no perder",
            "cómo clasifico este gasto",
            "esto es mercadería o insumo",
            "este gasto cómo lo cargo",
            "esto va como reventa o como gasto",
        ],
    },
    "analizar_proveedores": {
        "desc": "Analizar proveedores: ranking, comparación de precios o dependencia",
        "triggers": [
            "cómo están mis proveedores",
            "qué proveedor me conviene más",
            "dependo mucho de un solo proveedor",
        ],
    },
    "preparar_pedido_sugerido": {
        "desc": "Armar y guardar un borrador de pedido al proveedor desde el stock en quiebre",
        "triggers": [
            "armame un pedido",
            "preparame un pedido para el proveedor",
            "necesito reponer stock con un pedido",
            "armá un pedido a",
            "haceme un pedido al proveedor",
            "preparar un pedido de reposición",
        ],
    },
    "proyectar_caja": {
        "desc": "Proyectar flujo de caja, alertar falta de liquidez o simular un "
        "escenario financiero",
        "triggers": [
            "proyectá mi caja a 30 días",
            "cómo vengo de liquidez",
            "me parece que me voy a quedar sin plata",
            "qué pasa si vendo 20% menos el mes que viene",
            "simulá si contrato un empleado más",
        ],
    },
    "analizar_clientes": {
        "desc": "Análisis de clientes: mejores clientes, inactivos, cuentas por cobrar "
        "(fiado) y prioridad de cobranza",
        "triggers": [
            "quiénes son mis mejores clientes",
            "qué clientes no compran más",
            "cuánto me deben en total",
            "a quién le cobro primero",
        ],
    },
    # ── Sentinels (no clasificables; sin triggers) ────────────────────────────
    "pedir_aclaracion_sobre_archivo": {
        "desc": "Solicitar aclaración sobre intención con archivo adjunto",
        "triggers": [],
    },
    "pedir_aclaracion_negocio": {
        "desc": "Solicitar aclaración sobre mensaje de negocio ambiguo",
        "triggers": [],
    },
}

# ── Familias analíticas: intent consolidado → {default, analysis_type → _intent legacy} ──
# El handler de cada agente sigue despachando por el string legacy de `_intent`.
# `default` es el sub-análisis que corre cuando el CEO no extrae `analysis_type`
# (coincide con la rama fall-through ya existente en cada handler).
_ANALYTIC_FAMILIES: dict[str, dict[str, Any]] = {
    "analizar_precios": {
        "default": "analizar_margenes_productos",
        "types": {
            "margenes": "analizar_margenes_productos",
            "sugerencia": "sugerir_precios_venta",
            "comparacion": "comparar_listas_precios",
            "aumentos_proveedor": "detectar_aumentos_proveedor",
            "simulacion": "simular_actualizacion_precios",
            "sensibilidad": "identificar_productos_sensibles",
            "lista": "analizar_lista_precios",
        },
    },
    "analizar_stock": {
        "default": "analizar_stock",
        "types": {
            "general": "analizar_stock",
            "quiebres": "detectar_quiebres_stock",
            "sobrestock": "detectar_sobrestock",
            "reposicion": "priorizar_reposicion",
            "duracion": "estimar_dias_stock",
        },
    },
    "analizar_ventas": {
        "default": "analizar_rentabilidad_ventas",
        "types": {
            "rentabilidad": "analizar_rentabilidad_ventas",
            "estrella": "detectar_productos_estrella",
            "problematicos": "detectar_productos_problematicos",
            "ticket": "analizar_ticket_promedio",
            "descuentos": "analizar_descuentos",
        },
    },
    "analizar_clientes": {
        "default": "analizar_clientes",
        "types": {
            "general": "analizar_clientes",
            "inactivos": "detectar_clientes_inactivos",
            "cuentas_por_cobrar": "analizar_cuentas_por_cobrar",
            "cobranza": "priorizar_cobranza",
        },
    },
    "analizar_gastos": {
        "default": "clasificar_gastos",
        "types": {
            "clasificacion": "clasificar_gastos",
            "recurrentes": "detectar_gastos_recurrentes",
            "anomalos": "detectar_gastos_anomalos",
            "costos_fijos_variables": "analizar_costos_fijos_variables",
            "punto_equilibrio": "calcular_punto_equilibrio",
        },
    },
    "analizar_proveedores": {
        "default": "analizar_proveedores",
        "types": {
            "ranking": "analizar_proveedores",
            "comparacion_precios": "comparar_precios_proveedores",
            "dependencia": "detectar_dependencia_proveedor",
        },
    },
    "proyectar_caja": {
        "default": "proyectar_caja",
        "types": {
            "proyeccion": "proyectar_caja",
            "alerta_liquidez": "alertar_falta_liquidez",
            "what_if": "simular_escenario_financiero",
        },
    },
    "analizar_archivo": {
        "default": "analizar_archivo_cargado",
        "types": {
            "contenido": "analizar_archivo_cargado",
            "tipo": "detectar_tipo_archivo",
            "limpieza": "limpiar_normalizar_archivo",
            "resumen": "resumen_ejecutivo_archivo",
        },
    },
    "ayuda_plataforma": {
        "default": "ayuda_plataforma",
        "types": {
            "con_archivo": "ayudar_con_archivo",
            "explicar_datos": "explicar_que_puedo_hacer_con_datos",
        },
    },
}

# ── Aliases legacy → intent consolidado (red de seguridad para requests en vuelo) ──
# Si un key granular viejo llega (rescue antiguo, request cacheado durante el deploy),
# build_plan lo rutea al agente/ActionType correcto e inyecta el `_intent` legacy
# tal cual, preservando el comportamiento previo. Quitar cuando la telemetría
# confirme 0 uso.
_LEGACY_INTENT_ALIASES: dict[str, str] = {
    legacy: family_key
    for family_key, fam in _ANALYTIC_FAMILIES.items()
    for legacy in fam["types"].values()
    if legacy != family_key
}
_LEGACY_INTENT_ALIASES["generar_informe"] = "consultar_estado_negocio"

# ── Intent → agente especializado ─────────────────────────────────────────────
# Stage 2: los intents nuevos rutean a agent_income/agent_expense/agent_google.
# Los aliases legacy se conservan en registry.py para PendingActions históricas.
INTENT_TO_AGENT: dict[str, str] = {
    "ingresar_venta": "agent_income",
    "ingresar_cobro": "agent_income",
    "ingresar_gasto": "agent_expense",
    "ingresar_pago_salida": "agent_expense",
    "actualizar_stock": "agent_stock",
    "registrar_merma": "agent_stock",
    "actualizar_producto": "agent_stock",
    "importar_archivo_ventas": "agent_income",  # Stage 3: compound + (agent_stock, UPDATE_STOCK)
    "importar_archivo_gastos": "agent_expense",
    # importar_archivo_productos → agent_income: AgentStock no maneja IMPORT_TABULAR_FILE;
    # AgentIncome._maybe_build_uploaded_file_import detecta productos y arma el import.
    "importar_archivo_productos": "agent_income",
    # Stage 3: compound → (agent_stock, REGISTER_PURCHASE) + (agent_expense, REGISTER_CASH_OUTFLOW)
    "registrar_compra_proveedor": "agent_supplier",
    "reclasificar_gasto": "agent_expense",
    "consultar_estado_negocio": "agent_health",
    "generar_informe_con_export": "agent_health",  # Stage 4: DAG health → upload Drive
    "gestionar_proveedor": "agent_supplier",
    "sincronizar_google": "agent_google",
    "agendar_evento": "agent_google",
    "ayuda_plataforma": "agent_helper",
    "intent_desconocido": "agent_helper",
    # ── Familias analíticas consolidadas ──
    "analizar_archivo": "agent_income",
    "analizar_precios": "agent_stock",
    "analizar_stock": "agent_stock",
    "analizar_ventas": "agent_income",
    "analizar_gastos": "agent_expense",
    "analizar_proveedores": "agent_supplier",
    "preparar_pedido_sugerido": "agent_supplier",
    "proyectar_caja": "agent_health",
    "analizar_clientes": "agent_client",
    # ── Sentinels de aclaración ──
    "pedir_aclaracion_sobre_archivo": "agent_helper",
    "pedir_aclaracion_negocio": "agent_helper",
}

# ── Intent → ActionType (catálogo cerrado) ────────────────────────────────────
INTENT_TO_ACTION_TYPE: dict[str, ActionType] = {
    "ingresar_venta": ActionType.REGISTER_SALE,
    "ingresar_cobro": ActionType.REGISTER_CASH_INFLOW,
    "ingresar_gasto": ActionType.REGISTER_EXPENSE,
    "ingresar_pago_salida": ActionType.REGISTER_CASH_OUTFLOW,
    "actualizar_stock": ActionType.UPDATE_STOCK,
    "registrar_merma": ActionType.REGISTER_STOCK_LOSS,
    "actualizar_producto": ActionType.UPDATE_PRODUCT,
    "importar_archivo_ventas": ActionType.IMPORT_TABULAR_FILE,
    "importar_archivo_gastos": ActionType.IMPORT_TABULAR_FILE,
    "importar_archivo_productos": ActionType.IMPORT_TABULAR_FILE,
    "registrar_compra_proveedor": ActionType.REGISTER_PURCHASE,
    "reclasificar_gasto": ActionType.RECLASSIFY_EXPENSE,
    "consultar_estado_negocio": ActionType.GENERATE_HEALTH_REPORT,
    "generar_informe_con_export": ActionType.GENERATE_HEALTH_REPORT,  # Stage 4: primary action
    "gestionar_proveedor": ActionType.CREATE_SUPPLIER_DRAFT,
    "sincronizar_google": ActionType.SYNC_TO_GOOGLE,
    "agendar_evento": ActionType.CREATE_CALENDAR_EVENT,
    "ayuda_plataforma": ActionType.ANSWER_HELP_REQUEST,
    "intent_desconocido": ActionType.ANSWER_HELP_REQUEST,
    # ── Familias analíticas consolidadas ──
    "analizar_archivo": ActionType.ANALYZE_FILE,
    "analizar_precios": ActionType.ANALYZE_PRICES,
    "analizar_stock": ActionType.ANALYZE_STOCK_DATA,
    "analizar_ventas": ActionType.ANALYZE_SALES_DATA,
    "analizar_gastos": ActionType.ANALYZE_EXPENSE_DATA,
    "analizar_proveedores": ActionType.ANALYZE_SUPPLIER_DATA,
    "preparar_pedido_sugerido": ActionType.CREATE_PURCHASE_SUGGESTION,
    "proyectar_caja": ActionType.SIMULATE_SCENARIO,
    "analizar_clientes": ActionType.ANALYZE_SALES_DATA,
    # ── Sentinels de aclaración ──
    "pedir_aclaracion_sobre_archivo": ActionType.ANSWER_HELP_REQUEST,
    "pedir_aclaracion_negocio": ActionType.ANSWER_HELP_REQUEST,
}


# ── ActionTypes read-only que requieren conocer el sub-análisis (Sprint 17/19) ──
# Varios sub-análisis comparten un mismo ActionType analítico; el handler lee
# `_intent` desde entities para elegir el sub-análisis. Se incluye ANSWER_HELP_REQUEST
# para que ayuda_plataforma pueda diferenciar con_archivo / explicar_datos.
_INTENT_AWARE_ACTION_TYPES: frozenset[ActionType] = frozenset(
    {
        ActionType.ANALYZE_FILE,
        ActionType.ANALYZE_PRICES,
        ActionType.ANALYZE_STOCK_DATA,
        ActionType.ANALYZE_SALES_DATA,
        ActionType.ANALYZE_EXPENSE_DATA,
        ActionType.ANALYZE_SUPPLIER_DATA,
        ActionType.SIMULATE_SCENARIO,
        ActionType.ANSWER_HELP_REQUEST,
    }
)


def _resolve_legacy_discriminator(intent: str, analysis_type: Any) -> str:
    """Traduce (intent consolidado, analysis_type) → el `_intent` legacy del handler.

    - intent consolidado con analysis_type válido → sub-análisis mapeado.
    - intent consolidado sin analysis_type (o inválido) → default de la familia
      (la rama fall-through que el handler ya corre para mensajes ambiguos).
    - intent legacy granular (alias en vuelo) o no-analítico → se inyecta tal cual.
    """
    family = _ANALYTIC_FAMILIES.get(intent)
    if family is None:
        return intent
    types: dict[str, str] = family["types"]
    if isinstance(analysis_type, str) and analysis_type in types:
        return types[analysis_type]
    default: str = family["default"]
    return default


def _is_credit_purchase(entities: dict[str, Any]) -> bool:
    """Retorna True si las entidades indican una compra a crédito (no al contado)."""
    for val in entities.values():
        if isinstance(val, str):
            lowered = val.lower()
            if any(kw in lowered for kw in _CREDIT_KEYWORDS):
                return True
    return False


def _has_cobro_entity(entities: dict[str, Any]) -> bool:
    """Retorna True si las entidades incluyen datos de cobro simultáneo."""
    return bool(_COBRO_ENTITY_KEYS & set(entities.keys()))


def build_plan(intent: str, entities: dict[str, Any]) -> AgentTeamPlan:
    """Construye un AgentTeamPlan a partir del intent clasificado.

    Stage 3: genera planes compuestos (multi-task) para los siguientes intents:
    - importar_archivo_ventas    → (income, IMPORT_TABULAR_FILE) + (stock, UPDATE_STOCK) paralelo
    - registrar_compra_proveedor → (stock, REGISTER_PURCHASE)
      + (expense, REGISTER_CASH_OUTFLOW) si cash
    - ingresar_venta (con cobro) → (income, REGISTER_SALE)
      + (income, REGISTER_CASH_INFLOW) si cobro en entities

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
            depends_on=[],  # paralelo — sin dependencias entre sí
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
            depends_on=[task_stock.task_id],  # espera a que el stock se registre primero
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

    # ── generar_informe_con_export → DAG: health report + upload a Drive ─────
    if intent == "generar_informe_con_export":
        task_health = AgentTask(
            task_id=str(uuid.uuid4()),
            agent="agent_health",
            action_type=ActionType.GENERATE_HEALTH_REPORT,
            entities=entities,
            depends_on=[],
        )
        task_upload = AgentTask(
            task_id=str(uuid.uuid4()),
            agent="agent_google",
            action_type=ActionType.UPLOAD_TO_DRIVE,
            entities={**entities, "mode": "mcp"},
            depends_on=[task_health.task_id],  # espera el reporte antes de subir
        )
        return AgentTeamPlan(
            plan_id=plan_id,
            intent=intent,
            tasks=[task_health, task_upload],
            requires_synthesis=False,
        )

    # ── Caso general: single-task ─────────────────────────────────────────────
    # Resolver routing: intent consolidado o alias legacy en vuelo.
    routing_intent = intent
    if intent not in INTENT_TO_ACTION_TYPE and intent in _LEGACY_INTENT_ALIASES:
        routing_intent = _LEGACY_INTENT_ALIASES[intent]
    action_type = INTENT_TO_ACTION_TYPE.get(routing_intent, ActionType.ANSWER_HELP_REQUEST)
    agent = INTENT_TO_AGENT.get(routing_intent, "agent_helper")

    # Sprint 17/19: para ActionTypes analíticos (read-only) y de ayuda, inyectamos el
    # sub-análisis legacy en `_intent`. El handler lo lee para elegir el sub-análisis.
    # `analysis_type` (entidad) se traduce al string legacy; ausente → default de la
    # familia. Se inyecta SOLO en estos tipos read-only para no contaminar el
    # structured_data de las acciones de escritura (que termina en PendingActions).
    task_entities = entities
    if action_type in _INTENT_AWARE_ACTION_TYPES:
        discriminator = _resolve_legacy_discriminator(intent, entities.get("analysis_type"))
        task_entities = {**entities, "_intent": discriminator}

    task = AgentTask(
        task_id=str(uuid.uuid4()),
        agent=agent,
        action_type=action_type,
        entities=task_entities,
    )
    return AgentTeamPlan(
        plan_id=plan_id,
        intent=intent,
        tasks=[task],
        requires_synthesis=False,
    )
