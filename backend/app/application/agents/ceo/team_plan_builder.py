"""Catálogo de intents y constructor de AgentTeamPlan para AgentCEO.

Stage 1: solo emitía planes single-task.
Stage 3: genera planes compuestos para intents que requieren múltiples agentes.
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
# Para agregar un intent nuevo: (1) agregarlo aquí, (2) en INTENT_TO_AGENT,
# (3) en INTENT_TO_ACTION_TYPE, (4) en RiskEngine si usa un ActionType nuevo.
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
        "desc": "Ajustar unidades en inventario",
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
    "registrar_compra_proveedor": {
        "desc": "Registrar compra de mercadería a proveedor",
        "triggers": [
            "compré mercadería a Mayorista Norte",
            "llegó el pedido de la distribuidora",
            "recibí merca de mi proveedor",
            "compré stock por $20000",
        ],
    },
    # ── Informes y estado ─────────────────────────────────────────────────────
    "consultar_estado_negocio": {
        "desc": "Ver el score de salud financiera del negocio",
        "triggers": [
            "cómo está mi negocio",
            "score de salud financiera",
            "cómo ando financieramente",
            "dame un resumen del negocio",
            "cómo estoy",
        ],
    },
    "generar_informe": {
        "desc": "Generar informe de salud financiera completo",
        "triggers": [
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
        "desc": "Preguntas sobre cómo usar Véktor",
        "triggers": [
            "cómo uso Véktor",
            "qué puedo hacer con la plataforma",
            "tengo una duda sobre la app",
            "no entiendo cómo",
        ],
    },
    "intent_desconocido": {
        "desc": "Mensaje fuera del scope de Véktor o no clasificable",
        "triggers": [],
    },
    # ── Sprint 17: familia Archivos ───────────────────────────────────────────
    "analizar_archivo_cargado": {
        "desc": "Analizar qué hay en un archivo adjunto",
        "triggers": [
            "mirá este archivo que subí",
            "qué hay en esta planilla",
            "analizá esto",
            "revisá el archivo",
        ],
    },
    "importar_archivo_productos": {
        "desc": "Importar catálogo de productos desde archivo",
        "triggers": [
            "subí mi lista de precios",
            "cargar catálogo desde archivo",
            "importar mis productos del Excel",
        ],
    },
    "analizar_lista_precios": {
        "desc": "Analizar lista de precios adjunta vs catálogo",
        "triggers": [
            "analizá esta lista de precios",
            "revisá los precios que me mandó el proveedor",
            "qué onda esta lista de precios",
        ],
    },
    "detectar_tipo_archivo": {
        "desc": "Identificar qué tipo de datos tiene el archivo",
        "triggers": ["qué tipo de archivo es este", "qué subí", "identificá qué tiene esta planilla"],
    },
    "limpiar_normalizar_archivo": {
        "desc": "Limpiar y normalizar datos del archivo antes de importar",
        "triggers": [
            "limpiá esta planilla",
            "hay datos sucios en el archivo",
            "normalizá los datos",
        ],
    },
    "resumen_ejecutivo_archivo": {
        "desc": "Dar diagnóstico ejecutivo del archivo",
        "triggers": ["resumime este archivo", "dame un diagnóstico del archivo"],
    },
    # ── Sprint 17: familia Precios y márgenes ─────────────────────────────────
    "analizar_margenes_productos": {
        "desc": "Calcular y rankear márgenes por producto",
        "triggers": [
            "cuánto gano por producto",
            "qué margen tengo",
            "cuáles productos dejan más plata",
        ],
    },
    "sugerir_precios_venta": {
        "desc": "Sugerir precios para alcanzar margen objetivo",
        "triggers": [
            "a qué precio vendo esto para ganar bien",
            "sugerime un precio",
            "cómo remarco",
        ],
    },
    "comparar_listas_precios": {
        "desc": "Comparar dos listas de precios por cambios",
        "triggers": ["compará la lista vieja con la nueva", "qué cambió en la lista de precios"],
    },
    "detectar_aumentos_proveedor": {
        "desc": "Detectar aumentos en lista del proveedor vs costos actuales",
        "triggers": [
            "el proveedor me mandó una lista nueva, cuánto subió",
            "cuánto me aumentó la distribuidora",
        ],
    },
    "simular_actualizacion_precios": {
        "desc": "Simular impacto de aumentar precios en márgenes",
        "triggers": [
            "si remarcó 15% cómo quedan los márgenes",
            "simulá un aumento del 10%",
            "qué pasa si subo los precios",
        ],
    },
    "identificar_productos_sensibles": {
        "desc": "Detectar productos cuyo margen cae ante aumento de costo",
        "triggers": [
            "qué productos me quedan al límite si sube el costo",
            "dónde tengo que cuidar el precio",
        ],
    },
    # ── Sprint 17: familia Stock ──────────────────────────────────────────────
    "analizar_stock": {
        "desc": "Panorama general del inventario",
        "triggers": [
            "cómo está mi stock",
            "qué tengo en el depósito",
            "estado del inventario",
        ],
    },
    "detectar_quiebres_stock": {
        "desc": "Identificar productos sin stock o por agotarse",
        "triggers": [
            "qué me falta reponer",
            "me estoy quedando sin mercadería",
            "productos sin stock",
        ],
    },
    "detectar_sobrestock": {
        "desc": "Identificar productos con exceso de stock",
        "triggers": [
            "qué tengo de más",
            "capital inmovilizado en mercadería",
            "productos que no rotan",
        ],
    },
    "priorizar_reposicion": {
        "desc": "Ordenar productos por urgencia de reposición",
        "triggers": ["qué compro primero", "priorizá la reposición", "qué es urgente reponer"],
    },
    "estimar_dias_stock": {
        "desc": "Calcular para cuántos días alcanza el stock actual",
        "triggers": [
            "para cuántos días me alcanza el stock",
            "cuánto dura lo que tengo",
        ],
    },
    # ── Sprint 17: familia Ventas analíticas ──────────────────────────────────
    "analizar_rentabilidad_ventas": {
        "desc": "Calcular rentabilidad por producto en ventas",
        "triggers": ["qué productos me dejan más plata en ventas", "rentabilidad de mis ventas"],
    },
    "detectar_productos_estrella": {
        "desc": "Identificar productos con alto volumen y buen margen",
        "triggers": ["cuáles son mis productos estrella", "qué vendo más y me deja plata"],
    },
    "detectar_productos_problematicos": {
        "desc": "Identificar productos que venden mucho pero dejan poco",
        "triggers": ["qué productos vendo mucho pero me dejan poco", "mucho volumen bajo margen"],
    },
    "analizar_ticket_promedio": {
        "desc": "Calcular ticket promedio de ventas",
        "triggers": ["cuál es mi ticket promedio", "cuánto gasta por visita mi cliente"],
    },
    "analizar_descuentos": {
        "desc": "Analizar impacto de descuentos en el margen",
        "triggers": ["los descuentos me están comiendo el margen", "analizá mis descuentos"],
    },
    # ── Sprint 17: familia Gastos ─────────────────────────────────────────────
    "clasificar_gastos": {
        "desc": "Distribuir gastos por categoría",
        "triggers": [
            "cómo están distribuidos mis gastos",
            "desglosá mis egresos por categoría",
        ],
    },
    "detectar_gastos_recurrentes": {
        "desc": "Identificar gastos fijos que se repiten",
        "triggers": ["qué gastos fijos tengo", "mis suscripciones y alquileres"],
    },
    "detectar_gastos_anomalos": {
        "desc": "Detectar gastos inusuales o picos",
        "triggers": ["hay algún gasto raro o inusual", "gastos fuera de lo normal"],
    },
    "analizar_costos_fijos_variables": {
        "desc": "Separar costos fijos de variables",
        "triggers": ["separame costos fijos de variables", "mi estructura de costos"],
    },
    "calcular_punto_equilibrio": {
        "desc": "Calcular cuánto vender para no perder",
        "triggers": [
            "cuánto tengo que vender para no perder",
            "punto de equilibrio",
            "desde cuánto empiezo a ganar",
        ],
    },
    # ── Sprint 17: familia Proveedores ────────────────────────────────────────
    "analizar_proveedores": {
        "desc": "Ranking de proveedores por volumen de compra",
        "triggers": ["cómo están mis proveedores", "ranking de proveedores"],
    },
    "comparar_precios_proveedores": {
        "desc": "Comparar precios entre proveedores",
        "triggers": ["qué proveedor me conviene más", "compará precios entre proveedores"],
    },
    "detectar_dependencia_proveedor": {
        "desc": "Detectar concentración de compras en un proveedor",
        "triggers": ["dependo mucho de un solo proveedor", "riesgo de concentración de proveedor"],
    },
    "preparar_pedido_sugerido": {
        "desc": "Armar pedido sugerido para el proveedor",
        "triggers": ["armame un pedido para el proveedor", "qué debería pedirle al proveedor"],
    },
    # ── Sprint 17: familia Flujo de caja ──────────────────────────────────────
    "proyectar_caja": {
        "desc": "Proyectar flujo de caja a 30 días",
        "triggers": [
            "cuánta plata voy a tener la próxima semana",
            "proyectá mi caja a 30 días",
            "cómo vengo de liquidez",
        ],
    },
    "alertar_falta_liquidez": {
        "desc": "Detectar riesgo de quedarse sin liquidez",
        "triggers": [
            "me parece que me voy a quedar sin plata",
            "voy a tener problemas de caja próximamente",
        ],
    },
    "simular_escenario_financiero": {
        "desc": "Simular escenario financiero what-if",
        "triggers": [
            "qué pasa si vendo 20% menos el mes que viene",
            "simulá si contrato un empleado más",
        ],
    },
    # ── Sprint 17: familia Clientes (stub) ────────────────────────────────────
    "analizar_clientes": {
        "desc": "Análisis de clientes (stub — requiere campo cliente en ventas)",
        "triggers": ["quiénes son mis mejores clientes", "análisis de clientes"],
    },
    "detectar_clientes_inactivos": {
        "desc": "Detectar clientes que dejaron de comprar (stub)",
        "triggers": ["qué clientes no compran más", "clientes que se fueron"],
    },
    "analizar_cuentas_por_cobrar": {
        "desc": "Ver deudas pendientes de cobrar (stub)",
        "triggers": ["cuánto me deben en total", "facturas impagas", "deudas por cobrar"],
    },
    "priorizar_cobranza": {
        "desc": "Ordenar a quién cobrarle primero (stub)",
        "triggers": ["a quién le cobro primero", "prioridad de cobranza"],
    },
    # ── Sprint 17: familia Fallback / aclaración ──────────────────────────────
    "ayudar_con_archivo": {
        "desc": "Orientar al usuario sobre qué hacer con un archivo",
        "triggers": ["qué puedo hacer con este archivo", "ayudame con la planilla"],
    },
    "explicar_que_puedo_hacer_con_datos": {
        "desc": "Explicar capacidades de análisis para los datos del usuario",
        "triggers": ["qué podés hacer con estos datos", "explicame qué analizás"],
    },
    "pedir_aclaracion_sobre_archivo": {
        "desc": "Solicitar aclaración sobre intención con archivo adjunto",
        "triggers": [],
    },
    "pedir_aclaracion_negocio": {
        "desc": "Solicitar aclaración sobre mensaje de negocio ambiguo",
        "triggers": [],
    },
}

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
    # Stage 3: compound → (agent_stock, REGISTER_PURCHASE) + (agent_expense, REGISTER_CASH_OUTFLOW)
    "registrar_compra_proveedor": "agent_supplier",
    "consultar_estado_negocio": "agent_health",
    "generar_informe": "agent_health",
    "generar_informe_con_export": "agent_health",  # Stage 4: DAG health → upload Drive
    "gestionar_proveedor": "agent_supplier",
    "sincronizar_google": "agent_google",
    "agendar_evento": "agent_google",
    "ayuda_plataforma": "agent_helper",
    "intent_desconocido": "agent_helper",
    # ── Sprint 17: Archivos ──
    "analizar_archivo_cargado": "agent_income",
    # importar_archivo_productos → agent_income: AgentStock no maneja IMPORT_TABULAR_FILE;
    # AgentIncome._maybe_build_uploaded_file_import detecta productos y arma el import.
    "importar_archivo_productos": "agent_income",
    "analizar_lista_precios": "agent_stock",
    "detectar_tipo_archivo": "agent_income",
    "limpiar_normalizar_archivo": "agent_income",
    "resumen_ejecutivo_archivo": "agent_income",
    # ── Sprint 17: Precios y márgenes ──
    "analizar_margenes_productos": "agent_stock",
    "sugerir_precios_venta": "agent_stock",
    "comparar_listas_precios": "agent_stock",
    "detectar_aumentos_proveedor": "agent_stock",
    "simular_actualizacion_precios": "agent_stock",
    "identificar_productos_sensibles": "agent_stock",
    # ── Sprint 17: Stock ──
    "analizar_stock": "agent_stock",
    "detectar_quiebres_stock": "agent_stock",
    "detectar_sobrestock": "agent_stock",
    "priorizar_reposicion": "agent_stock",
    "estimar_dias_stock": "agent_stock",
    # ── Sprint 17: Ventas analíticas ──
    "analizar_rentabilidad_ventas": "agent_income",
    "detectar_productos_estrella": "agent_income",
    "detectar_productos_problematicos": "agent_income",
    "analizar_ticket_promedio": "agent_income",
    "analizar_descuentos": "agent_income",
    # ── Sprint 17: Gastos ──
    "clasificar_gastos": "agent_expense",
    "detectar_gastos_recurrentes": "agent_expense",
    "detectar_gastos_anomalos": "agent_expense",
    "analizar_costos_fijos_variables": "agent_expense",
    "calcular_punto_equilibrio": "agent_expense",
    # ── Sprint 17: Proveedores ──
    "analizar_proveedores": "agent_supplier",
    "comparar_precios_proveedores": "agent_supplier",
    "detectar_dependencia_proveedor": "agent_supplier",
    "preparar_pedido_sugerido": "agent_supplier",
    # ── Sprint 17: Flujo de caja ──
    "proyectar_caja": "agent_health",
    "alertar_falta_liquidez": "agent_health",
    "simular_escenario_financiero": "agent_health",
    # ── Sprint 17: Clientes (stub) ──
    "analizar_clientes": "agent_income",
    "detectar_clientes_inactivos": "agent_income",
    "analizar_cuentas_por_cobrar": "agent_income",
    "priorizar_cobranza": "agent_income",
    # ── Sprint 17: Fallback / aclaración ──
    "ayudar_con_archivo": "agent_helper",
    "explicar_que_puedo_hacer_con_datos": "agent_helper",
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
    "registrar_compra_proveedor": ActionType.REGISTER_PURCHASE,
    "consultar_estado_negocio": ActionType.GENERATE_HEALTH_REPORT,
    "generar_informe": ActionType.GENERATE_HEALTH_REPORT,
    "generar_informe_con_export": ActionType.GENERATE_HEALTH_REPORT,  # Stage 4: primary action
    "gestionar_proveedor": ActionType.CREATE_SUPPLIER_DRAFT,
    "sincronizar_google": ActionType.SYNC_TO_GOOGLE,
    "agendar_evento": ActionType.CREATE_CALENDAR_EVENT,
    "ayuda_plataforma": ActionType.ANSWER_HELP_REQUEST,
    "intent_desconocido": ActionType.ANSWER_HELP_REQUEST,
    # ── Sprint 17: Archivos ──
    "analizar_archivo_cargado": ActionType.ANALYZE_FILE,
    "importar_archivo_productos": ActionType.IMPORT_TABULAR_FILE,
    "analizar_lista_precios": ActionType.ANALYZE_PRICES,
    "detectar_tipo_archivo": ActionType.ANALYZE_FILE,
    "limpiar_normalizar_archivo": ActionType.ANALYZE_FILE,
    "resumen_ejecutivo_archivo": ActionType.ANALYZE_FILE,
    # ── Sprint 17: Precios y márgenes ──
    "analizar_margenes_productos": ActionType.ANALYZE_PRICES,
    "sugerir_precios_venta": ActionType.ANALYZE_PRICES,
    "comparar_listas_precios": ActionType.ANALYZE_PRICES,
    "detectar_aumentos_proveedor": ActionType.ANALYZE_PRICES,
    "simular_actualizacion_precios": ActionType.ANALYZE_PRICES,
    "identificar_productos_sensibles": ActionType.ANALYZE_PRICES,
    # ── Sprint 17: Stock ──
    "analizar_stock": ActionType.ANALYZE_STOCK_DATA,
    "detectar_quiebres_stock": ActionType.ANALYZE_STOCK_DATA,
    "detectar_sobrestock": ActionType.ANALYZE_STOCK_DATA,
    "priorizar_reposicion": ActionType.ANALYZE_STOCK_DATA,
    "estimar_dias_stock": ActionType.ANALYZE_STOCK_DATA,
    # ── Sprint 17: Ventas analíticas ──
    "analizar_rentabilidad_ventas": ActionType.ANALYZE_SALES_DATA,
    "detectar_productos_estrella": ActionType.ANALYZE_SALES_DATA,
    "detectar_productos_problematicos": ActionType.ANALYZE_SALES_DATA,
    "analizar_ticket_promedio": ActionType.ANALYZE_SALES_DATA,
    "analizar_descuentos": ActionType.ANALYZE_SALES_DATA,
    # ── Sprint 17: Gastos ──
    "clasificar_gastos": ActionType.ANALYZE_EXPENSE_DATA,
    "detectar_gastos_recurrentes": ActionType.ANALYZE_EXPENSE_DATA,
    "detectar_gastos_anomalos": ActionType.ANALYZE_EXPENSE_DATA,
    "analizar_costos_fijos_variables": ActionType.ANALYZE_EXPENSE_DATA,
    "calcular_punto_equilibrio": ActionType.ANALYZE_EXPENSE_DATA,
    # ── Sprint 17: Proveedores ──
    "analizar_proveedores": ActionType.ANALYZE_SUPPLIER_DATA,
    "comparar_precios_proveedores": ActionType.ANALYZE_SUPPLIER_DATA,
    "detectar_dependencia_proveedor": ActionType.ANALYZE_SUPPLIER_DATA,
    "preparar_pedido_sugerido": ActionType.ANALYZE_SUPPLIER_DATA,
    # ── Sprint 17: Flujo de caja ──
    "proyectar_caja": ActionType.SIMULATE_SCENARIO,
    "alertar_falta_liquidez": ActionType.SIMULATE_SCENARIO,
    "simular_escenario_financiero": ActionType.SIMULATE_SCENARIO,
    # ── Sprint 17: Clientes (stub) ──
    "analizar_clientes": ActionType.ANALYZE_SALES_DATA,
    "detectar_clientes_inactivos": ActionType.ANALYZE_SALES_DATA,
    "analizar_cuentas_por_cobrar": ActionType.ANALYZE_SALES_DATA,
    "priorizar_cobranza": ActionType.ANALYZE_SALES_DATA,
    # ── Sprint 17: Fallback / aclaración ──
    "ayudar_con_archivo": ActionType.ANSWER_HELP_REQUEST,
    "explicar_que_puedo_hacer_con_datos": ActionType.ANSWER_HELP_REQUEST,
    "pedir_aclaracion_sobre_archivo": ActionType.ANSWER_HELP_REQUEST,
    "pedir_aclaracion_negocio": ActionType.ANSWER_HELP_REQUEST,
}


# ── ActionTypes read-only que requieren conocer el intent específico (Sprint 17) ──
# Varios intents mapean al mismo ActionType analítico; el handler lee `_intent`
# desde entities para elegir el sub-análisis. Se incluye ANSWER_HELP_REQUEST para
# que la familia de fallback (ayudar_con_archivo, explicar_que_puedo_hacer_con_datos)
# también pueda diferenciarse en el helper.
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
    action_type = INTENT_TO_ACTION_TYPE.get(intent, ActionType.ANSWER_HELP_REQUEST)
    agent = INTENT_TO_AGENT.get(intent, "agent_helper")

    # Sprint 17: para intents analíticos (read-only) y de aclaración, inyectamos el
    # intent específico en `_intent`. Varios intents comparten un mismo ActionType
    # (ej. ANALYZE_PRICES cubre 6 intents) y el handler necesita distinguirlos.
    # Se inyecta SOLO en estos tipos read-only para no contaminar el structured_data
    # de las acciones de escritura (que termina en PendingActions).
    task_entities = entities
    if action_type in _INTENT_AWARE_ACTION_TYPES:
        task_entities = {**entities, "_intent": intent}

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
