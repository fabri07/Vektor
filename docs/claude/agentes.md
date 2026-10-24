# Capa de agentes LLM, memoria y MCP Google — detalle

> Detalle de dominio movido desde `CLAUDE.md` (que tiene que quedar bajo 40k caracteres). No se carga solo: **leelo antes de tocar este dominio.** Regla de siempre: un cambio de comportamiento, una migración o un límite que deja de ser cierto se documenta acá **en el mismo commit**.

## Capa de Agentes LLM (`app/application/agents/`)

10 sub-agentes coordinados por AgentCEO + AgentChat (capa de respuesta). El cliente NUNCA elige el agente destino. En el registry hay 11 entradas (`agent_cash` es alias de `agent_income`).

| Agente | Context Budget | Responsabilidad |
|--------|---------------|-----------------|
| AgentCEO | 2.000 tokens | Router/coordinador — nunca accede a datos de negocio |
| AgentIncome | 3.000 tokens | Ventas, cobros, ingresos, import archivos de ventas |
| AgentExpense | 3.000 tokens | Gastos, pagos, salidas de caja, import archivos de gastos |
| AgentStock | 3.000 tokens | Inventario, quiebres, rotación, merma, actualizar productos |
| AgentSupplier | 3.500 tokens | Proveedores, Gmail vía MCP |
| AgentHealth | 4.000 tokens | Score de salud, narrativa ejecutiva |
| AgentHelper | 2.500 tokens | FAQ, manual, guía funcional |
| AgentGoogle | 4.000 tokens | Google Calendar + Sheets + Docs vía MCP |
| **AgentClient** (v4 F2/F6a) | — | Análisis de clientes (facturación, inactivos, cuentas por cobrar, priorizar cobranza) + cobranza WhatsApp (`PREPARE_WHATSAPP_MESSAGE`, LOCAL/MEDIUM, `requires_approval`; el link `wa.me` lo ejecuta `PendingActionService`). `agent_client` en el registry. |
| **AgentMarketing** (v4 F4) | — | Marketing read-only y determinístico (`ANALYZE_MARKETING_DATA`): dashboard ads/ventas, ROI de ads, sugerir campaña. El LLM nunca calcula — cifras desde `MarketingService` + `shared/analytics`. `agent_marketing` en el registry. |
| **AgentChat** | — | **Capa de respuesta (Sprint 18):** sintetiza el `AgentResponse` del sub-agente + contextos en lenguaje natural rioplatense. NUNCA accede a la DB. Reemplazó a `_generate_rich_response` del ChatOrchestrator. `agent_chat` en el registry. |

> Verificar `backend/app/application/agents/<agent>/agent.py` antes de asumir estado del agente.
> Stage 5d completado: `agent_calendar` y `agent_sync` eliminados del registry. Solo queda el alias `agent_cash` → `agent_income` (backward-compat para `PendingActions` en vuelo con target viejo).

**Modelos LLM (Sprint 18 — migración Haiku → Sonnet):** todos los agentes usan `claude-sonnet-4-6` (CEO, Income, Expense, Stock, Supplier, Helper, Google, AgentChat, AgentHealth sub_narrator y `ceo/synthesis.py`). max_tokens subidos en la migración (CEO 300→800, Income 300→800, Stock hasta 800, Supplier 600→1200, Helper 400→800, synthesis 600→1200, AgentChat 1200). La única mención de `claude-haiku-4-5-20251001` que queda es un comentario de ejemplo en `shared/schemas.py`. Verificar cada agente antes de asumir el modelo.

**Cliente Anthropic:** todos los agentes via `get_anthropic_async_client()`. No instanciar `anthropic.AsyncAnthropic` directo.

**Contratos fijos** (`app/application/agents/shared/schemas.py`):
- `AgentRequest`: `{ request_id, user_id, business_id, message, attachments, conversation_id }` — sin `agent_target`
- `AgentResponse`: `{ request_id, agent_name, status, risk_level, requires_approval, confidence, result, pending_action_id?, pending_action_ids?, approval_group_id?, question?, message?, usage? }` — `pending_action_ids`/`approval_group_id` implementados en Stage 3 (multi-task)
- `AgentRequest.context`: dict reservado para outputs upstream del DAG (`{"upstream_outputs": {task_id: result_dict}}`). Vacío en single-task y en el primer nivel.
- `status`: `"success" | "requires_approval" | "requires_clarification" | "requires_google_auth" | "error"`
- `confidence`: `"HIGH" | "MEDIUM" | "LOW"` — nunca float
- `LLMCall`: `{ source, model, input_tokens, output_tokens }`. `UsageSummary`: `{ calls: list[LLMCall] }` + `total_input/output/total`.

**ActionType** (`shared/schemas.py`) — catálogo cerrado de 31 valores:

```
REGISTER_SALE          REGISTER_CASH_INFLOW    REGISTER_EXPENSE
REGISTER_PURCHASE      REGISTER_CASH_OUTFLOW   UPDATE_STOCK
UPDATE_PRODUCT         REGISTER_STOCK_LOSS     CREATE_PURCHASE_SUGGESTION
IMPORT_TABULAR_FILE    PARSE_DOCUMENT_FILE     GENERATE_HEALTH_REPORT
ANSWER_HELP_REQUEST    CREATE_SUPPLIER_DRAFT   CLASSIFY_GMAIL_MESSAGE
SYNC_TO_GOOGLE         CREATE_CALENDAR_EVENT
# Stage 4 — Google writes via GoogleToolBroker
UPLOAD_TO_DRIVE        CREATE_GOOGLE_DOC       APPEND_TO_SHEET
# Sprint 17 — analíticos read-only (LOW risk, sin aprobación, los LLM no calculan)
ANALYZE_FILE           ANALYZE_PRICES          ANALYZE_STOCK_DATA
ANALYZE_SALES_DATA     ANALYZE_EXPENSE_DATA    ANALYZE_SUPPLIER_DATA
SIMULATE_SCENARIO
# Reclasificación de gastos vía chat (MEDIUM, requiere aprobación)
RECLASSIFY_EXPENSE
# v4 — analítico de marketing read-only (LOW)
ANALYZE_MARKETING_DATA
# v4 F5 — capa consultiva (LOW, read-only, los LLM narran pero no calculan)
ANSWER_DATA_QUERY
# v4 F6a — cobranza WhatsApp click-to-chat (MEDIUM, requiere aprobación)
PREPARE_WHATSAPP_MESSAGE
```

Agregar/quitar requiere actualizar `RiskEngine` y sus tests.

**Auto-ejecución:** `REGISTER_EXPENSE` sin confirmación (`_AUTO_EXECUTE_ACTION_TYPES` en `api/v1/agent.py`). Prefijo "Gasto registrado.". **Desde el chat sólo escribe quien confirma** (`agents/shared/permisos_chat.py`: `ROLES_QUE_EJECUTAN` = OWNER/ADMIN, los mismos de `/agent/confirm`; R-1, revisión profunda 2026-10-24). Para cualquier otro rol (VIEWER, ANALYST), una respuesta que escribiría algo —auto-ejecución (`REGISTER_EXPENSE`), acción para aprobar, o un plan con alguna tarea así— se reemplaza por `MENSAJE_SIN_PERMISO` («Tu usuario es de solo lectura…»; `result.blocked_by_role = True`): **no se ejecuta ni queda pendiente**, y tampoco se buscan reglas automáticas. Antes un VIEWER que escribía «gasté 5000 en luz» dejaba el gasto persistido. No se deja pendiente porque nadie la aprobaría: vence a los 10 minutos y el dueño no tiene una bandeja de pendientes ajenas. Dos capas: la HTTP (`_process_agent_action`, en `/agent/chat` y `/agent/chat/stream`) es la garantía; el orquestador (`ChatOrchestrator.handle(..., role_code=)`) lo aplica antes de narrar, así no llama a AgentChat y el turno guardado dice lo que pasó. **Límite:** el orquestador sin `role_code` (ningún llamador actual) no filtra; la capa HTTP sí. Compuerta: `test_chat_autoejecucion_por_rol.py`.

**Compras por chat (B·5b-1):** `REGISTER_PURCHASE` pasa por `compra_service.registrar_compra` (la regla única de compra): sin producto o sin cantidad no registra nada (`CompraSinArticulosError`, que el chat muestra tal cual), y el gasto queda con producto y proveedor. `REGISTER_EXPENSE` y `REGISTER_CASH_OUTFLOW` con categoría mercadería se rechazan igual: una compra de mercadería dice qué artículos y cuántos. El catálogo de `ActionType` no cambia.

**Deduplicación por fingerprint** (`operation_fingerprints`): SHA-256 de `{tenant_id}:{action_type}:{amount}:{date}` para `REGISTER_SALE/EXPENSE/PURCHASE`. Duplicado → `execution_status: "DUPLICATE"`.

**Rate limit:** 50 mensajes/día por tenant. Redis key: `rate:chat:{tenant_id}:{date}`. Compartido entre `/chat` y `/chat/stream`.

**RiskEngine** (`shared/risk_engine.py`) — determinístico, sin LLM. `HIGH`/`MEDIUM` requieren aprobación; `LOW` no.

**Retry** (`POST /agent/retry/{pending_id}`): solo `status=APPROVED`, `execution_status in (FAILED, REQUIRES_RECONNECT)`, `is_external=True`, máximo 1 reintento.

**`execution_status`:** `IN_PROGRESS` → `SUCCEEDED | FAILED | REQUIRES_RECONNECT`. `REQUIRES_RECONNECT` = `McpToolAuthError`; frontend guía a `/apps`.

**ContextBuilder** (`shared/context_builder.py`) — prioridades de contexto (en orden):
1. `historical_data` (400t) → `conversation_history` (1000t) → `recent_events` (800t) → `current_snapshot` (600t, siempre) → `uploaded_files` (400t) → `agent_memory` (300t) → `business_heuristics` (300t, siempre) → `intent_and_entities` (200t, siempre)

**HeuristicEngine** (`shared/heuristic_engine.py`): `get(business_type)` síncrono; `get_async(...)` aplica `BusinessHeuristicOverride` de la DB. `to_prompt_fragment()` genera valores numéricos para system prompts — nunca texto narrativo. Fallback a `kiosco_almacen`.

**AgentCEO — flujo:** `nlp_preprocessor` (Sprint 18, `agents/shared/nlp_preprocessor.py`) normaliza lunfardo/rioplatense (merca, birra, remarcar, guita, etc.) antes del LLM; spacy (`es_core_news_sm`) opcional con fallback a regex puro; las entidades NLP se inyectan como anotación pre-análisis en el prompt del CEO. Luego `classify_intent()` → LLM (max_tokens=800) → **33 intents** del `INTENT_CATALOG` en español rioplatense (incluye `intent_desconocido`, `reclasificar_gasto` → `agent_expense` → `RECLASSIFY_EXPENSE`, y los 4 de v4: `analizar_clientes`/`analizar_marketing` → familias analíticas, `consulta_libre` → `ANSWER_DATA_QUERY`, `recordar_por_whatsapp` → `PREPARE_WHATSAPP_MESSAGE`). `INTENT_CATALOG` es `dict[str, {desc, triggers}]` y el CEO construye el system prompt dinámicamente desde el catálogo. **Sprint 19 (consolidación):** las 8 familias analíticas son UN intent cada una; el sub-análisis va en la entidad `analysis_type` (best-effort). `build_plan()` traduce `(intent, analysis_type)` → el `_intent` legacy que el handler espera vía `_resolve_legacy_discriminator()` (default por familia si falta `analysis_type`); el mapeo vive en `_ANALYTIC_FAMILIES`. `_LEGACY_INTENT_ALIASES` resuelve keys granulares en vuelo (red de seguridad). Luego `INTENT_TO_ACTION_TYPE` + `INTENT_TO_AGENT` (determinísticos, en `ceo/team_plan_builder.py`) → `build_plan()` → `AgentTeamPlan` → `registry.get_sub_agent(name)`. Intent `actualizar_producto` → `agent_stock` → `UPDATE_PRODUCT` action.

**Rescate de intent (Sprint 17):** cuando el CEO devuelve `intent_desconocido`, ChatOrchestrator NO corta inmediatamente. Aplica dos capas determinísticas (sin LLM) en `_rescue_unknown_intent()`: (1) `DataIntentExtractor` sobre los adjuntos parseados → si hay datos importables, mapea a `analizar_precios`(`analysis_type=lista`)/`analizar_archivo`; (2) `intent_rescue.rescue_intent()` (en `shared/intent_rescue.py`, devuelve `tuple[intent, entities]` con `analysis_type`) — scoring semántico de verbo ambiguo + objeto de negocio + tipo de adjunto, con normalización de voseo/tildes y fuzzy matching (`rapidfuzz`, umbral 85) para typos. Solo si ambas fallan corta: `out_of_scope` (off-topic claro → mensaje de scope) o `pedir_aclaracion_negocio`/`pedir_aclaracion_sobre_archivo` (suena a negocio pero ambiguo → pide detalle). Constantes `_NO_AGENT_INTENTS`/`_NO_AGENT_MESSAGES` en `chat_orchestrator.py`. Para los 7 ActionTypes analíticos (+`ANSWER_HELP_REQUEST`), `build_plan()` inyecta el sub-análisis en `entities["_intent"]` (resuelto desde `analysis_type`) para que el handler distinga sub-análisis dentro de un mismo ActionType.

**Handlers analíticos (Sprint 17):** read-only, LOW risk, sin aprobación. Math determinística centralizada en `shared/analytics.py` (funciones puras: márgenes, días de stock, sobrestock, estrella/problemático, anomalías de gasto, punto de equilibrio). Los agentes cargan datos vía repos y arman un `message` determinístico — los LLM NUNCA calculan. Familias: AgentStock (`ANALYZE_PRICES`, `ANALYZE_STOCK_DATA`), AgentIncome (`ANALYZE_FILE`, `ANALYZE_SALES_DATA` + clientes stub), AgentExpense (`ANALYZE_EXPENSE_DATA`), AgentSupplier (`ANALYZE_SUPPLIER_DATA`), AgentHealth (`SIMULATE_SCENARIO`: proyección de caja vía ForecastService + what-if vía DeterministicFinance). Queries nuevas: `product_repository.get_products_with_margin`, `sale_repository.get_daily_velocity`/`get_sales_by_product`, `expense_repository.get_expense_stats_by_category`.

**Capa consultiva v4 (F5) — `ANSWER_DATA_QUERY`:** consulta libre en lenguaje natural (LOW, read-only). 7 dominios, cada uno resuelto por su agente: caja/ventas (AgentIncome), gastos (AgentExpense), stock (AgentStock), proveedores (AgentSupplier), clientes (AgentClient), marketing (AgentMarketing), salud (AgentHealth). Intent `consulta_libre`. Los agentes cargan datos vía repos + `shared/stats_engine.py` (numpy/numpy-financial) e inyectan estadística determinística en `structured_data["analisis"]`; la narrativa la genera `shared/data_query_narrator.py` — **el LLM narra pero nunca calcula** (F7 cableó el `stats_engine` a los 7 dominios). `stats_enrichment.py` arma el enriquecimiento; STOCK concentra sobre valor `units×cost` (no `margin_pct`, porque HHI requiere montos aditivos).

**Cobranza WhatsApp v4 (F6a) — `PREPARE_WHATSAPP_MESSAGE`:** LOCAL y MEDIUM (requiere aprobación). AgentClient determina el destinatario (cliente con mayor saldo si no se especifica), arma el cuerpo determinístico y emite `requires_approval`. La ejecución genera un link `wa.me` (click-to-chat, sin API de WhatsApp) en `PendingActionService`. Intent `recordar_por_whatsapp`.

**AgentTeamPlan / AgentTask** (`shared/schemas.py`): `AgentTeamPlan { plan_id, intent, tasks: list[AgentTask], requires_synthesis, fallback_message? }`. `AgentTask { task_id, agent, action_type, entities, depends_on: list[task_id], approval_group? }`. Stage 3 completado: `build_plan()` soporta planes multi-task y DAGs; ejecución con skip-downstream ante fallo; `agents/ceo/synthesis.py` genera narrativa sintetizada de múltiples resultados (`claude-sonnet-4-6` desde Sprint 18).

**GoogleToolBroker** (`agents/google/tool_broker.py`): ejecuta las 3 acciones Google de escritura (`UPLOAD_TO_DRIVE`, `CREATE_GOOGLE_DOC`, `APPEND_TO_SHEET`) via MCP. `PendingActionService` lo llama al confirmar acciones externas de AgentGoogle.

**Streaming SSE** (`POST /agent/chat/stream`): `{"type": "thinking"}` → `{"type": "response", "data": AgentResponse}` → `{"type": "error"}`. Finaliza con `data: [DONE]`. Frontend: `sendStream()` con placeholder `thinkingId` que se actualiza in-place.

**ChatOrchestrator** (`app/application/services/chat_orchestrator.py`): carga 4 capas de contexto (fail-silencioso): negocio + heurísticas, BusinessMemory, AgentMemory, últimos 5 UploadedFile. CEO clasifica → despacha sub-agente → si `requires_google_auth` retorna sin LLM → si `requires_approval` usa summary estructurado → **delega la generación de la respuesta a AgentChat** (Sprint 18; `_generate_rich_response`/`_format_agent_result`/`_render_session_memory`/`_format_history_turn` se movieron a AgentChat y se eliminaron del orchestrator). Acumula todas las `LLMCall` en `all_llm_calls` → `AgentResponse.usage`. Siempre inyecta `intent` + `target_agent` en `result`.

**ConversationService**: Redis caché caliente (TTL 24h) + PostgreSQL. Ventana deslizante 10 turnos. `conversation_id` = UUID generado en el cliente.

**EventBus** (`shared/event_bus.py`): wrapper sobre Celery `send_task`. Sin suscripción CEO en código — coordinación via tasks/event handlers.

**AgentHealth v2 — sub-pipeline interno** (`agents/health/`):
- `sub_collector.py` — recolecta `BusinessState` vía BSL (misma fuente que Celery)
- `sub_calculator.py` — calcula `ComponentScoresV2` (fórmula: `cash×0.30 + stock×0.20 + supplier×0.10 + margin×0.20 + growth×0.20`)
- `sub_narrator.py` — genera narrativa ejecutiva con `claude-sonnet-4-6`
- `scorer.py` — shim de compatibilidad con la API anterior

**AgentHelper — contrato `redirect_to`:** si el usuario pregunta algo del negocio (fuera del scope del manual), AgentHelper devuelve `result["redirect_to"] = "main_chat"`. Si AgentCEO detecta pregunta de ayuda de plataforma, devuelve `result["redirect_to"] = "help"`. Frontend `/help` usa `HelpChat.tsx`; endpoint: `POST /agent/help/chat` (sin rate limit, sin token billing de sesión).

**Extras:** `agents/supplier/preflight.py` — validaciones pre-envío Gmail.

## Sistema de Memoria (cuatro capas)

| Capa | Servicio | TTL | Qué almacena |
|------|----------|-----|--------------|
| Conversacional | `ConversationService` | 24h Redis + PG | Últimos 10 turnos |
| Negocio | `BusinessMemoryService` | 5min | Resumen financiero (ventas, gastos, alertas) |
| Agente | `AgentMemoryService` | 5min | Patrones: método de pago, monto promedio (Welford), top ActionTypes |
| Sesión chat | `ChatMemoryService` | permanente PG | Eventos de carga de datos (DATA_LOADED, DATA_REJECTED, FILE_UPLOADED, INTENT_DETECTED, QUERY_ANSWERED) |

`AgentMemoryService.record_action()` post-acción confirmada (fail-silent). Confianza: `min(1.0, 0.5 + count/20)`.
**Tabla:** `agent_memory` — `(tenant_id, key)` unique, `value` JSONB, `occurrence_count`, `confidence`.
**`ChatMemoryService`**: escribe en `chat_session_log` (migración `20260503_0002`). Permite saber qué se cargó en sesiones previas sin depender del historial conversacional.

## Integración MCP Google

Feature flag: `ENABLE_GOOGLE_MCP_TOOLS=false` (default). Variables propias: `GOOGLE_MCP_OAUTH_CLIENT_ID/SECRET/REDIRECT_URI` — redirect al MCP server, no al backend.

**Port/adapter:**
- `app/application/ports/mcp_gateway.py` — interface `McpToolGateway`
- `app/integrations/mcp/http_gateway.py` — `HttpMcpGateway` (HTTP JSON-RPC 2.0)
- `app/integrations/mcp/google_mcp_service.py` — `GoogleMcpService` con allowlist por agente
- `app/integrations/mcp/exceptions.py` — `McpToolAuthError`

**Allowlists por agente:** frozensets de `google.gmail.*`, `google.calendar.*`, `google.sheets.*`, `google.docs.*`.

**Router `integrations`** (`app/api/v1/integrations.py`):
- `GET /integrations/google/status` — estado (`CONNECTING/CONNECTED/DISCONNECTED/ERROR`)
- `POST /integrations/google/connect/start` — inicia OAuth; crea `GoogleMcpConnection(status=CONNECTING)`
- `POST /integrations/google/disconnect` — revoca tokens

**ORM:** `GoogleMcpConnection` (`app/persistence/models/google_mcp_connection.py`) — `google_mcp_connections`, cols clave: `tenant_id`, `user_id`, `status`, `scopes_granted` (JSONB), `connected_at`.

**`google_oauth_tokens`** — gestionada por el MCP server directamente. Callback fallido → `last_error_code` en `mcp_server/app/auth/service.py`. `CONNECTING` > 10 min expira como `oauth_callback_timeout`.

**Prompt defense:** todo input de usuario a LLM pasa por `wrap_user_input()`. `is_valid_action_type()` valida output LLM contra catálogo cerrado.
