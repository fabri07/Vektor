# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

---

## Proyecto

Véktor es un SaaS de salud financiera para PYMEs argentinas (kioscos, decoración hogar, limpieza). Multi-tenant, monolito modular en v1.

### Estado actual importante

- El chat productivo entra por `ChatOrchestrator` (no despacha directo router→sub-agente). Clientes Anthropic vía `app/integrations/anthropic_client.py` (no instanciar `anthropic.AsyncAnthropic()` directo).
- Integraciones Google son **MCP-based** (`ENABLE_GOOGLE_MCP_TOOLS` + `MCP_SERVER_URL`); `Google Login` social vía `/auth/oauth/google/*` es independiente. Frontend `/apps`: conexión, estado, reconnect ante `REQUIRES_RECONNECT`.
- Pipeline de archivos centralizado en `file_parsing.py`: uploads de chat = parseo síncrono; ingestión = pipeline propio con confirmación humana.
- Frontend: tema dark unificado (`vektor-night`/`vektor-ink`/`vektor-surface`/`vektor-border`), tipografías `Barlow Condensed` + `Inter`. Landing pública (`/`): hero full-screen + social proof + highlights. Dashboard dividido: `/dashboard` (health score + KPIs) y `/dashboard/analisis` (charts + insights), launchpad compartido.
- Ticker económico vía `GET /api/economia`. Insights de charts `GET /insights/current` (template-based; el stub `/api/analisis/insight` ya no se usa).
- Benchmarks de margen desde JSON (`heuristics/verticals/loader.py → load_margin_benchmark()`); el health engine no importa los .py de vertical; alias `kiosco` → `kiosco_almacen` en el loader.
- `HealthAlertBanner.tsx`: alertas fixed bottom-right si `score < 75` + `risk_code` activo (CASH_LOW/MARGIN_LOW/STOCK_CRITICAL/SUPPLIER_DEPENDENCY), dismissable. `SmartTable<T>`: selector de columnas + export CSV UTF-8 BOM (`defaultVisible`, `csvValue`).
- `GET /insights/breakdown?days=N`: gastos por categoría + top 5 proveedores + stock crítico. `GET /forecast/cash`: 3 tiers (T1 <30d promedio 14d, T2 30–90d EWMA, T3 90d+ patrón semanal), Redis TTL 6h, `?refresh=true`.
- `analytics_events`: log insert-only anonimizado por vertical (sin `tenant_id`); `compute_margin_benchmark()` usa `percentile_cont` con ≥5 muestras/90 días; `GET /admin/analytics/benchmarks` (SUPERADMIN). `decision_audit_log`: `tokens_input/output/total` + `decision_data.{ceo_target_agent,sub_agent_name,token_calls}` (`20260429_0001`).
- **DeterministicFinance** + **ValidationGate** (Sprint 11): toda aritmética financiera va por `deterministic_finance.py` (LLMs nunca calculan); `validation_gate.py` rechaza `confidence=LOW` antes de que el agente procese datos.
- **Provenance** (`20260503_0001`): `provenance VARCHAR(10)` en `sales_entries`/`expense_entries` — `"demo"`/`"manual"`/`"import"`. **ChatMemoryService** (`chat_memory_service.py`): 4ª capa de memoria, log durable por sesión en `chat_session_log` (entry types `DATA_LOADED/DATA_REJECTED/FILE_UPLOADED/INTENT_DETECTED/QUERY_ANSWERED`). **Agent Automation Rules** (`automation_service.py`): consentimiento explícito para auto-ejecutar, flag `ENABLE_AGENT_AUTOMATIONS`, API `/automations`.
- **Custom Fields** (Sprint 12): `custom_fields JSONB` en `sales_entries`, `expense_entries`, `products`, `business_profiles`. Base en `vertical_field_definitions`; overrides en `tenant_custom_field_definitions`; undo en `tenant_field_change_log`. API `/fields`. Frontend `FieldDefinitionsPanel` + `SchemaERDView` en `/settings`.
- **Soft delete auditado** (Sprint 13, `20260510_0001`/`20260511_0001`): `voided_at` + `void_reason VARCHAR(30)` (`REPAIR_MISCLASSIFIED_IMPORT`/`USER_REQUEST`/`DATA_QUALITY`) en ventas/gastos/productos. **DataRepairService** (`data_repair_service.py`): detecta `SaleEntry` de CSVs de productos mal clasificados, anula ventas y reconstruye `Product`; flujo `dry_run` → preview → `apply`; traza en `data_repair_runs`/`data_repair_items`; API admin `POST /admin/repairs/misclassified-product-imports/{dry-run,apply}` (SUPERADMIN).
- **Product lookup en AgentCash** (Sprint 13): `_lookup_product_price()` fuzzy match contra catálogo; si es ambiguo pregunta antes de registrar. `product_id` se inyecta en la entidad; el PATCH de ventas lo acepta y recalcula el monto.
- **Estados canónicos de producto** (Sprint 14): `ProductResponse.stock_status` computed `"in_stock"|"low_stock"|"out_of_stock"` → badges + filtros URL (aliases `ok/low/out` siguen). Chip "En camino" es stub (count=0 hasta `purchase_orders`).
- **UPDATE_PRODUCT vía chat** (Sprint 14): `ActionType.UPDATE_PRODUCT` (MEDIUM). Intent `update_product` → AgentStock `PRODUCT_UPDATE`; distingue `sku` (identificador) de `new_sku` (campo a actualizar). `PendingActionService` ejecuta via `ProductRepository`; recalcula score solo si cambian `sale_price_ars/unit_cost_ars/stock_units`. Regla FUENTE DE VERDAD: Google Sheets solo para import/export, nunca para modificar.
- **Política null/NaN en imports** (Sprint 14): helpers en `file_parsing.py` (`normalize_numeric`, `normalize_categorical`, `impute_column` quantity→0/numeric→mediana/categorical→None, `flag_columns_at_risk` umbral 35%); `_parse_spreadsheet` calcula `columns_at_risk` en `parsed_summary_json`. Validators `amount_no_nan` en `Create{Sale,Expense}Request`.
- **Ingesta interactiva columnas riesgosas** (Sprint 14): `GET /ingestion/files/{id}/preview` expone `columns_at_risk[]`. `POST /files/{id}/drop-columns` elimina columnas de todos los buckets; `POST /files/{id}/cancel` → `NEEDS_COMPLETION`. Frontend `FileListSection` con panel por columna >35% nulos.
- **Filtros temporales jerárquicos** (Sprint 20): `lib/period.ts` (`PeriodValue` preset/year/month/week/day, `resolvePeriod`/`resolvePreviousPeriod`/`formatPeriodLabel`) + `PeriodFilter.tsx` (navegación año→mes→semana→día). `GET /sales/date-range` + `/expenses/date-range` (min/max, declarados antes de `/{id}`). Productos no tiene dimensión temporal (catálogo).
- **Horarios laborales** (Sprint 20): `WorkScheduleService` + `/settings/work-schedule`; onboarding Step2 con validación "todo o nada". TZ ART hardcoded.
- **Cierre de caja diario / arqueo** (Sprint 20): modelo `CashClose` + `CashCloseService` con neteo de efectivo server-side; router `cash_closes`. Integrado en informe semanal (fila cierres + diferencia acumulada).
- **Time-series intradía** (Sprint 22): `transaction_date` es DATETIME (captura hora) en ventas/gastos; toda agregación diaria usa `func.date(transaction_date)` para comportamiento por-día idéntico (la tarde del último día NO se excluye de rangos `to_date=hoy`). Agentes income/expense default `datetime.now()`. `products.acquired_at` (fecha de alta editable). Frontend `lib/datetime.ts` + granularidad "Hoy (horas)".

---

## Quick start (fresh clone)

```bash
# Backend
cd backend && cp .env.example .env && make dev && make seed-demo
# Frontend (otra terminal)
cd frontend && cp .env.local.example .env.local && npm install && npm run dev
```

Abrir `http://localhost:3000/demo` para entrar con un tenant demo.

---

## Comandos de desarrollo

### Backend (desde `backend/`)

```bash
make dev / dev-bg / stop / logs / shell        # Docker Compose hot reload
make test                                       # pytest + cobertura (mín 50%)
make test-fast / test-watch
make test-file FILE=app/tests/api/v1/test_auth.py
pytest app/tests/api/v1/test_auth.py::test_login -v
make lint / format / typecheck / check          # Calidad
make migrate / migrate-down / migrate-history    # Migraciones (dir: app/persistence/migrations/versions/, NO alembic/versions/)
make migrate-create MSG="descripcion"
make migrate-neon                                # aplica migrations contra Neon (valida URL); usar en prod
make seed-demo / reset-demo
make db-reset                                    # ⚠️ PELIGROSO
```

### Frontend (desde `frontend/`)

```bash
npm run dev / build / lint / type-check / test
```

### Variables de entorno

- Backend: `backend/.env.example` → `backend/.env`. Frontend: `frontend/.env.local.example` → `frontend/.env.local` (`NEXT_PUBLIC_API_URL`).
- En producción: Railway/Vercel, nunca commitear.

---

## Arquitectura del backend

### Flujo de datos principal

`HTTP → deps.py (JWT decode + tenant_id) → Router (api/v1/) → Application Service → BSL (agrega 30 días) → Health Engine (domain/health_score.py) → Persistence (repository) → decision_audit_log (insert-only, siempre) → Celery (score recalc async, post-write)`.

**Regla crítica:** Datos crudos NUNCA llegan al Health Engine directamente. Todo pasa por `BusinessStateLayer.compute()` → `BusinessState` con 5 scores (0–100): `liquidity`, `profitability`, `cost_control`, `sales_momentum`, `debt_coverage`.

### Capas y responsabilidades

- **API** `app/api/v1/` — routing, validación Pydantic, auth deps. **Deps** `deps.py` — JWT decode, `get_current_user/tenant`, `require_role()`.
- **Application** `app/application/services/` — orquestación: auth, cash, conversation, google_oauth, health_score, health_config, onboarding, pending_action, score_trigger, stock, supplier, business_memory, agent_memory, forecast, analytics, deterministic_finance, validation_gate, chat_memory, field_definition, automation, data_intent_extractor, ingestion_import, data_repair, report_export, team_plan_executor, help_documentation, work_schedule, cash_close, column_mapping.
- **Commands/Queries** (CQRS) · **DTOs** `app/application/dto/` · **DB middleware** `db/tenant_context.py` (inyecta tenant_id) · **Agents** `app/application/agents/` (multiagente LLM).
- **Domain** `app/domain/` (entidades puras) · **BSL** `app/state/business_state_layer.py` (revenue/expenses 30d → 5 scores) · **Heuristics** `app/heuristics/` · **Persistence** `app/persistence/` (SQLAlchemy async, repos, Alembic) · **Jobs** `app/jobs/` (Celery) · **Security** `app/application/security/` (`prompt_defense.py`/`wrap_user_input()`).

### API Routers (`app/api/v1/`)

Registrados en `router.py`. Dominios: `auth`, `oauth`, `tenants`, `users`, `business_profiles`, `sales`, `expenses`, `products`, `health_scores`, `insights`, `momentum`, `notifications`, `files`, `ingestion`, `onboarding`, `agent`, `integrations`, `forecast`, `admin`, `fields`, `automations`, `settings`, `cash_closes`.

**Router `settings`** (`app/api/v1/settings.py`):
- `GET/PATCH/DELETE /settings/health-config` — margen del tenant; PATCH (OWNER/ADMIN) valida `target_margin_pct >= warning_margin_pct`, rango [0.0, 80.0]; DELETE resetea al vertical.
- `GET/PATCH /settings/work-schedule` (Sprint 20) — días/horarios (`work_days` JSONB 0-6, `work_open_hour`, `work_close_hour`); `WorkScheduleService.resolve_schedule()` null-check explícito, default Lun-Sáb 09-18; audit `WORK_SCHEDULE_UPDATED`.

**Router `cash_closes`** (Sprint 20): `GET /cash-closes/preview` (esperado por método con **neteo de efectivo** server-side: ventas cash − gastos cash, inflow aparte; `is_past_close_now` vía `ZoneInfo` ART), `POST /cash-closes` (arqueo, 409 doble cierre, RBAC, `difference = counted − expected`), `GET /cash-closes` (historial). Modelo `CashClose` unique `(tenant_id, fecha)`; frontend `CashCloseButton` + `CashCloseModal`.

**`POST /health-scores/{snapshot_id}/export`** — informe PDF/DOCX vía `report_export_service.py`. Body `{format: "pdf"|"docx", narrative: str}`.

### Autenticación y multi-tenancy

- JWT (HS256, python-jose), payload `sub`/`tenant_id`/`role_code`. `get_current_tenant_id` en TODOS los endpoints de negocio — `tenant_id` viene del JWT, nunca del body/path.
- Roles `OWNER`/`ADMIN`/`VIEWER` vía `require_role(...)`. En producción `/docs`, `/redoc`, `/openapi.json` deshabilitados.

### Celery

Queues: `default`, `scores`, `notifications`, `reports`, `ingestion`. Post-write: `trigger_score_recalculation.delay(str(tenant_id), triggered_by="...")`. Beat: momentum update + weekly email (lunes 08:00 ART).

### Scores: dos sistemas distintos

**`ScoreLevel`** (`app/domain/health_score.py`) — clasifica `total_score`:

| Rango | ScoreLevel | | Rango | Severidad (notif) |
|-------|-----------|--|-------|-----------|
| 90–100 | `EXCELLENT` | | ≥80 | `LOW` |
| 75–89 | `GOOD` | | ≥60 | `MEDIUM` |
| 60–74 | `FAIR` | | ≥30 | `HIGH` |
| 40–59 | `WARNING` | | <30 | `CRITICAL` |
| 0–39 | `CRITICAL` | | | |

`HealthScore.needs_attention` → `True` si `level in (CRITICAL, WARNING)`.
**`severity_from_score()`** en `app/heuristics/insight_templates.py` — severidad de notificación.
> Los dos sistemas son **intencionalmente distintos** y no deben unificarse.

### Heuristics e Insights

- Insights **template-based** (no LLM) en `app/heuristics/insight_templates.py`. Risk codes: `CASH_LOW`, `MARGIN_LOW`, `STOCK_CRITICAL`, `SUPPLIER_DEPENDENCY`.
- Benchmarks canónicos `app/application/data/heuristics/{kiosco_almacen,limpieza,decoracion_hogar}.json` (`critical_below`/`warning_below`/`healthy_min`/`healthy_max`); con ≥5 muestras/90 días `HealthScoreService` usa el benchmark data-driven.

### Capa de Agentes LLM (`app/application/agents/`)

8 sub-agentes coordinados por AgentCEO + AgentChat (capa de respuesta). El cliente NUNCA elige el agente destino. Registry: 9 entradas (`agent_cash` alias de `agent_income`). Clientes Anthropic vía `get_anthropic_async_client()` (no instanciar `AsyncAnthropic` directo).

Context budgets (tokens): **AgentCEO** 2.000 (router/coordinador, nunca accede a datos), **AgentIncome** 3.000 (ventas/cobros/ingresos, import ventas), **AgentExpense** 3.000 (gastos/pagos/salidas, import gastos), **AgentStock** 3.000 (inventario/quiebres/rotación/merma, update productos), **AgentSupplier** 3.500 (proveedores, Gmail vía MCP), **AgentHealth** 4.000 (score + narrativa ejecutiva), **AgentHelper** 2.500 (FAQ/manual), **AgentGoogle** 4.000 (Calendar/Sheets/Docs vía MCP).
**AgentChat** (Sprint 18, sin budget fijo): capa de respuesta — sintetiza `AgentResponse` + contextos en rioplatense, NUNCA accede a la DB; reemplazó `_generate_rich_response`.

> Verificar `app/application/agents/<agent>/agent.py` antes de asumir estado. `agent_calendar`/`agent_sync` eliminados (Stage 5d); único alias `agent_cash` → `agent_income` (backward-compat para `PendingActions` en vuelo).

**Modelos LLM (Sprint 18 — migración Haiku → Sonnet):** TODOS los agentes usan `claude-sonnet-4-6` (CEO, Income, Expense, Stock, Supplier, Helper, Google, AgentChat, AgentHealth `sub_narrator`, `ceo/synthesis.py`) con max_tokens subidos. La única mención de `claude-haiku-4-5-20251001` que queda es un comentario de ejemplo en `shared/schemas.py`.

**Contratos fijos** (`shared/schemas.py`):
- `AgentRequest`: `{ request_id, user_id, business_id, message, attachments, conversation_id }` (sin `agent_target`); `.context` reservado para outputs upstream del DAG (`{"upstream_outputs": {task_id: result_dict}}`, vacío en single-task).
- `AgentResponse`: `{ request_id, agent_name, status, risk_level, requires_approval, confidence, result, pending_action_id?, pending_action_ids?, approval_group_id?, question?, message?, usage? }` (`pending_action_ids`/`approval_group_id` desde Stage 3).
- `status`: `"success" | "requires_approval" | "requires_clarification" | "requires_google_auth" | "error"`. `confidence`: `"HIGH" | "MEDIUM" | "LOW"` (nunca float).
- `LLMCall`: `{ source, model, input_tokens, output_tokens }`. `UsageSummary`: `{ calls: list[LLMCall] }` + `total_input/output/total`.

**ActionType** (`shared/schemas.py`) — catálogo cerrado de 27 valores:

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
```

Agregar/quitar requiere actualizar `RiskEngine` y sus tests.

- **Auto-ejecución:** `REGISTER_EXPENSE` sin confirmación (`_AUTO_EXECUTE_ACTION_TYPES` en `api/v1/agent.py`); prefijo "Gasto registrado.".
- **Dedup por fingerprint** (`operation_fingerprints`): SHA-256 de `{tenant_id}:{action_type}:{amount}:{date}` para `REGISTER_SALE/EXPENSE/PURCHASE` → duplicado = `execution_status: "DUPLICATE"`.
- **Rate limit:** 50 msg/día por tenant, Redis `rate:chat:{tenant_id}:{date}`, compartido `/chat` + `/chat/stream`.
- **RiskEngine** (`shared/risk_engine.py`): determinístico; `HIGH`/`MEDIUM` requieren aprobación, `LOW` no.
- **Retry** (`POST /agent/retry/{pending_id}`): solo `status=APPROVED`, `execution_status in (FAILED, REQUIRES_RECONNECT)`, `is_external=True`, máx 1 reintento.
- **`execution_status`:** `IN_PROGRESS` → `SUCCEEDED | FAILED | REQUIRES_RECONNECT` (= `McpToolAuthError`; frontend guía a `/apps`).

**ContextBuilder** (`shared/context_builder.py`) — prioridades: `historical_data` (400t) → `conversation_history` (1000t) → `recent_events` (800t) → `current_snapshot` (600t, siempre) → `uploaded_files` (400t) → `agent_memory` (300t) → `business_heuristics` (300t, siempre) → `intent_and_entities` (200t, siempre).

**HeuristicEngine** (`shared/heuristic_engine.py`): `get(business_type)` síncrono; `get_async(...)` aplica `BusinessHeuristicOverride` de la DB. `to_prompt_fragment()` genera valores numéricos (nunca texto narrativo). Fallback a `kiosco_almacen`.

**AgentCEO — flujo:** `nlp_preprocessor` (`agents/shared/nlp_preprocessor.py`) normaliza lunfardo/rioplatense (merca, birra, remarcar, guita) antes del LLM; spacy (`es_core_news_sm`) opcional con fallback a regex; entidades NLP se inyectan como anotación pre-análisis. Luego `classify_intent()` → LLM (max_tokens=800) → **28 intents** del `INTENT_CATALOG` (`dict[str, {desc, triggers}]`, system prompt dinámico; incluye `intent_desconocido`). Sprint 19: las 8 familias analíticas son UN intent; el sub-análisis va en la entidad `analysis_type`. `build_plan()` traduce `(intent, analysis_type)` → `_intent` legacy vía `_resolve_legacy_discriminator()` (mapeo en `_ANALYTIC_FAMILIES`; `_LEGACY_INTENT_ALIASES` red de seguridad). `INTENT_TO_ACTION_TYPE` + `INTENT_TO_AGENT` (determinísticos, `ceo/team_plan_builder.py`) → `AgentTeamPlan` → `registry.get_sub_agent(name)`.

**Rescate de intent (Sprint 17, ampliado 21):** ante `intent_desconocido` (o `pedir_aclaracion_sobre_archivo` con adjuntos), `_rescue_unknown_intent()` aplica dos capas sin LLM: (1) `DataIntentExtractor` sobre los adjuntos (incluye `inferred_type="general"`) → datos importables mapean a `analizar_precios`/`analizar_archivo` o import; (2) `intent_rescue.rescue_intent()` (`shared/intent_rescue.py`, devuelve `tuple[intent, entities]`) — scoring semántico verbo + objeto de negocio + tipo de adjunto, normalización voseo/tildes + fuzzy (`rapidfuzz`, umbral 85). Default: archivo de tipo conocido sin verbo analítico → import; sin tipo ni verbo → `analizar_archivo`. Solo si ambas fallan corta: `out_of_scope` (off-topic) o `pedir_aclaracion_negocio`/`_sobre_archivo`. Constantes `_NO_AGENT_INTENTS`/`_NO_AGENT_MESSAGES` en `chat_orchestrator.py`.

**Handlers analíticos (Sprint 17):** read-only, LOW risk, sin aprobación. Math determinística en `shared/analytics.py` (funciones puras: márgenes, días de stock, sobrestock, estrella/problemático, anomalías de gasto, punto de equilibrio) — los LLM NUNCA calculan. Familias: AgentStock (`ANALYZE_PRICES`, `ANALYZE_STOCK_DATA`), AgentIncome (`ANALYZE_FILE`, `ANALYZE_SALES_DATA`), AgentExpense (`ANALYZE_EXPENSE_DATA`), AgentSupplier (`ANALYZE_SUPPLIER_DATA`), AgentHealth (`SIMULATE_SCENARIO`: forecast + what-if vía DeterministicFinance). Queries nuevas en repos: `get_products_with_margin`, `get_daily_velocity`/`get_sales_by_product`, `get_expense_stats_by_category`.

**AgentTeamPlan / AgentTask** (`shared/schemas.py`): `AgentTeamPlan { plan_id, intent, tasks, requires_synthesis, fallback_message? }`; `AgentTask { task_id, agent, action_type, entities, depends_on, approval_group? }`. Stage 3: `build_plan()` soporta multi-task y DAGs; ejecución con skip-downstream ante fallo; `agents/ceo/synthesis.py` sintetiza narrativa de múltiples resultados.

**GoogleToolBroker** (`agents/google/tool_broker.py`): ejecuta las 3 acciones Google de escritura (`UPLOAD_TO_DRIVE`, `CREATE_GOOGLE_DOC`, `APPEND_TO_SHEET`) via MCP; lo invoca `PendingActionService` al confirmar acciones externas.

**Streaming SSE** (`POST /agent/chat/stream`): `{"type":"thinking"}` → `{"type":"response","data":AgentResponse}` → `{"type":"error"}`, finaliza `data: [DONE]`. Frontend `sendStream()` con placeholder `thinkingId` in-place.

**ChatOrchestrator** (`app/application/services/chat_orchestrator.py`): carga 4 capas de contexto (fail-silencioso): negocio + heurísticas, BusinessMemory, AgentMemory, últimos 5 UploadedFile. CEO clasifica → despacha sub-agente → si `requires_google_auth` retorna sin LLM → si `requires_approval` usa summary estructurado → **delega la respuesta a AgentChat** (Sprint 18; `_generate_rich_response`/`_format_agent_result`/`_render_session_memory`/`_format_history_turn` movidos a AgentChat). Acumula `LLMCall` en `all_llm_calls` → `AgentResponse.usage`. Siempre inyecta `intent` + `target_agent` en `result`.

**ConversationService**: Redis caché caliente (TTL 24h) + PostgreSQL. Ventana deslizante 10 turnos. `conversation_id` = UUID generado en el cliente.

**EventBus** (`shared/event_bus.py`): wrapper sobre Celery `send_task`. Sin suscripción CEO en código — coordinación via tasks/event handlers.

**AgentHealth v2 — sub-pipeline interno** (`agents/health/`): `sub_collector.py` (recolecta `BusinessState` vía BSL, misma fuente que Celery) → `sub_calculator.py` (`ComponentScoresV2`, fórmula `cash×0.30 + stock×0.20 + supplier×0.10 + margin×0.20 + growth×0.20`) → `sub_narrator.py` (narrativa con `claude-sonnet-4-6`). `scorer.py` = shim de compatibilidad.

**AgentHelper — contrato `redirect_to`:** pregunta de negocio (fuera del manual) → `result["redirect_to"] = "main_chat"`; pregunta de ayuda de plataforma detectada por CEO → `"help"`. Frontend `/help` (`HelpChat.tsx`); endpoint `POST /agent/help/chat` (sin rate limit, sin token billing).

**Extras:** `agents/supplier/preflight.py` — validaciones pre-envío Gmail.

### Sistema de Memoria (cuatro capas)

- **Conversacional** `ConversationService` (24h Redis + PG): últimos 10 turnos.
- **Negocio** `BusinessMemoryService` (5min): resumen financiero (ventas, gastos, alertas).
- **Agente** `AgentMemoryService` (5min): patrones — método de pago, monto promedio (Welford), top ActionTypes.
- **Sesión chat** `ChatMemoryService` (permanente PG): eventos de carga (`DATA_LOADED/DATA_REJECTED/FILE_UPLOADED/INTENT_DETECTED/QUERY_ANSWERED`).

`AgentMemoryService.record_action()` post-acción confirmada (fail-silent); confianza `min(1.0, 0.5 + count/20)`; tabla `agent_memory` (`(tenant_id, key)` unique, `value` JSONB, `occurrence_count`, `confidence`). `ChatMemoryService` escribe en `chat_session_log` (`20260503_0002`) — qué se cargó en sesiones previas sin depender del historial conversacional.

### Integración MCP Google

Feature flag: `ENABLE_GOOGLE_MCP_TOOLS=false` (default). Variables propias: `GOOGLE_MCP_OAUTH_CLIENT_ID/SECRET/REDIRECT_URI` — redirect al MCP server, no al backend.

**Port/adapter** (`app/integrations/mcp/`): `McpToolGateway` (port en `app/application/ports/`) → `HttpMcpGateway` (JSON-RPC 2.0) → `GoogleMcpService` con allowlist por agente (frozensets `google.{gmail,calendar,sheets,docs}.*`); `McpToolAuthError` en `exceptions.py`.

**Router `integrations`** (`app/api/v1/integrations.py`): `GET /integrations/google/status` (`CONNECTING/CONNECTED/DISCONNECTED/ERROR`), `POST /integrations/google/connect/start` (crea `GoogleMcpConnection(status=CONNECTING)`), `POST /integrations/google/disconnect`.

**ORM:** `GoogleMcpConnection` → `google_mcp_connections` (`tenant_id`, `user_id`, `status`, `scopes_granted` JSONB, `connected_at`). **`google_oauth_tokens`** la gestiona el MCP server directamente; callback fallido → `last_error_code`; `CONNECTING` > 10 min → `oauth_callback_timeout`.

**Prompt defense:** todo input de usuario a LLM pasa por `wrap_user_input()`. `is_valid_action_type()` valida output LLM contra catálogo cerrado.

### Archivos e ingestión

**`file_parsing.py`** centraliza: sanitización, detección MIME, `parsed_summary_json`, helpers chat (`summary_columns`, `summary_row_count`, `preview_value_from_summary`).

**Chat upload** (`POST /files/upload?purpose=chat`): parseo síncrono → `UploadedFile(processing_status=DONE)`. Fallo → 422, sin persistir.

**Ingestión** (`api/v1/ingestion.py` + `jobs/ingestion_worker.py`): Upload → Parse async (Celery) → Preview → Confirm (humano obligatorio).

**Mapeo de columnas** (Sprints 21–22): `ColumnMappingService` sugiere destino por columna con 3 capas (historial del tenant en `tenant_column_mappings` → heurística → fuzzy). `GET /ingestion/files/{id}/column-mappings` (sugerencias), `GET /ingestion/column-mappings` (aliases aprendidos), `DELETE /ingestion/column-mappings/{id}` (olvidar). `confirm_file` valida campos requeridos (422 si falta `amount`/`transaction_date`/`name`), crea `TenantCustomFieldDefinition` idempotente para mapeos `custom_field:{key}` (`ensure_custom_field_exists()`), persiste aprendizaje post-confirm. `ConfirmIngestionRequest.column_mappings` opcional (backward compat).

**Mapeo universal por contexto** (Sprint 22): modelo `mapping_contexts` + marcador `__context__` en el summary → mapeo por hoja/grupo en **todos** los formatos (xlsx multi-hoja, csv, y texto: txt/pdf/docx/pptx/ocr vía `_build_text_contexts`). `ColumnMapping.context_id/entity_type`, `ConfirmIngestionRequest.context_confirmed`. `confirm_file` agrupa por contexto; `_insert_multisheet_data` itera por contexto con mapeo explícito + fallback por keyword. Frontend `ColumnMapperPanel` con `SheetMapperSection` + `MultiContextMapper`. Single-sheet/csv conserva el flujo legacy.

**Tablas dinámicas desde field-definitions** (Sprint 22): `lib/customFields.ts` (`formatCustomFieldValue` + `buildCustomFieldColumns`); `/sales`, `/products`, `/expenses` agregan columnas custom (ocultas por defecto, mismo data source que el ERD).

**Remediación de ingestión (FASE 0–1, en branches `feat/fase0-...`/`feat/fase1-...`):** observabilidad — tabla `pipeline_events` (traza append-only por `trace_id`/stage vía `pipeline_event_service`, migración `20260701_0001`), `trace_id` propagado al worker; `DELETE /files` ahora es **soft-delete** que preserva el crudo en R2 (cols `trace_id`/`content_hash`/`deleted_at` en `uploaded_files`); admin `GET /admin/pipeline/{stats,trace}`. Parser robusto — detección de delimitador CSV (`,;\t|` vía `csv.Sniffer`), encoding/BOM (`utf-8-sig`/latin-1), `_detect_header_row` (salta títulos), filas irregulares toleradas, **sin truncamiento `[:50]`**. Robustez — cap unificado **16 MB** (`MAX_FILE_SIZE_BYTES` en `file_parsing`), hojas multi-hoja no clasificables preservadas (`entity_type=None` + warning, no se descartan), confirm sin mapeos que no matchea → **422 explícito** (no inserta 0 en silencio), dedup de re-upload por `content_hash` (`UploadResponse.duplicate_of`).

### Observabilidad

`structlog` (`from app.observability.logger import get_logger`); `bind_request_context(tenant_id, user_id)` en `deps.py` por request; rate limiting `slowapi` (200 req/min).

---

## Arquitectura del frontend

- Next.js 15 App Router (protegidas `(protected)/`, públicas `(public)/`). Estado global Zustand (`src/stores/`); server-state TanStack Query. HTTP axios wrapper `src/lib/api.ts`. UI Tailwind + `src/components/ui/`; charts Recharts; forms Zod.

### Estado visual

- Tema dark: `globals.css` + `tailwind.config.ts` (tokens `vektor-*`). Tipografías en `layout.tsx`: `Barlow Condensed` (UI) + `Inter` (texto).
- `EconomicTicker` desde `(protected)/layout.tsx` solo en dashboard (mobile → botón/modal). `DashboardLaunchpadNav` tabs/dots compartidos entre `/dashboard` y `/dashboard/analisis`.

### Organización del frontend

- `src/features/` — auth, chat, dashboard, onboarding, ingestion, notifications.
- `src/services/` — HTTP por dominio (auth, sales, expenses, products, health_score, dashboard, momentum, notifications, ingestion, onboarding, files, agent).
- `src/stores/` — `authStore` (JWT + user), `toastStore`. `src/types/api.ts` — tipos de respuestas API. `AuthHydrationBoundary.tsx` — hidrata auth desde localStorage.

### Rutas protegidas (`src/app/(protected)/`)

- `/chat` (**home post-login**, chat completo) · `/dashboard` (health score hero + KPIs) · `/dashboard/analisis` (charts + ForecastPanel + breakdown gastos + stock crítico).
- `/sales` (analytics + lista + `PeriodFilter` + `CashCloseButton`) · `/expenses` (+ `PeriodFilter`) · `/products` (catálogo, `?stock=ok|low|out`) · `/apps` (Google).
- `/settings` (cuenta, custom fields `FieldDefinitionsPanel`+`SchemaERDView`, margen `HealthConfigPanel`, horarios `WorkSchedulePanel`) · `/help` (`HelpChat.tsx`, `/agent/help/chat`).

**Ruta pública:** `/oauth/callback?session_id=` → `POST /auth/oauth/google/exchange`.

### Chat

- `/chat` = home post-login (`ChatPanel.tsx` existe pero no en layout global). `conversation_id` = UUID de `useChatStore.newConversation()` (no del servidor).
- Adjuntos `AttachmentPicker.tsx` (hasta 3, upload inmediato a `POST /files/upload?purpose=chat`). `useChat` expone `send` (REST) + `sendStream` (SSE); `MUTATING_ACTIONS` = 7 types que invalidan queries.

### Google OAuth login federado / MCP (frontend)

- Login federado: `POST /auth/oauth/google/start` → `/oauth/callback?session_id=` → `POST /auth/oauth/google/exchange`; si `link_required`, vincular con contraseña vía `POST /auth/oauth/google/link-pending`.
- `/apps` gestiona conexión/reconexión/desconexión Google. `REQUIRES_RECONNECT` → guía a `/apps`. `GOOGLE_MCP_OAUTH_REDIRECT_URI` registrado exacto en Google Cloud Console. El botón abre ventana en blanco y redirige al llegar `auth_url` (evita popup blocker).

### Endpoints frontend recientes

- `GET /api/economia` — dólar oficial/blue/MEP/CCL + inflación/REM/BCRA (`revalidate=1800`)
- `GET /forecast/cash` → `fetchCashForecast()` → `CashForecastResponse/ForecastPoint`
- `GET /insights/breakdown?days=N` → `BusinessBreakdownResponse`
- `GET /health-scores/history/v2` → `HealthScoreV2Response[]`
- `GET /insights/current` → `InsightBlock` (el stub `/api/analisis/insight` ya no se usa)
- `GET /sales|/expenses/date-range`, `GET /cash-closes/preview` → `PeriodFilter` + cierre de caja

---

## Historial de sprints

| Sprint | Estado | Descripción |
|--------|--------|-------------|
| 1–4 | ✅ | Auth social Google, OAuth frontend, primeras integraciones, Pending Actions externas |
| 5 | ✅ | Chat como home, OAuth federado, adjuntos, analytics Ventas/Gastos/Productos |
| 6 | ✅ | MCP Google: AgentCalendar + AgentSync, BusinessMemory, AgentMemory, file context |
| 7 | ✅ | Data moat: heurísticas JSON, alertas accionables, SmartTable+CSV, forecast 3-tiers, analytics_events |
| 8 | ✅ | Token tracking: LLMCall/UsageSummary, decision_audit_log tokens_*, dashboard cablea endpoints reales |
| 9 | ✅ | Hardening MCP, RBAC sales/expenses/agent, narrativa LLM en insights, cash-breakdown, margin-history |
| 10 | ✅ | Observabilidad, DeterministicFinance, ValidationGate, configuración segura, motor financiero robusto |
| 11 | ✅ | ETL Hardening: provenance tagging, ChatMemoryService, chat unificado con memoria persistente, agent automation rules |
| 12 | ✅ | Custom fields por vertical: `vertical_field_definitions`, `tenant_custom_field_definitions`, undo log, panel en `/settings` + ERD |
| 13 | ✅ | Datos editables/borrables unificados: soft delete auditado en ventas/gastos, sistema de reparación de importaciones mal clasificadas, product lookup en AgentCash |
| 14 | ✅ | Estados canónicos de producto (in_stock/low_stock/out_of_stock/incoming stub), UPDATE_PRODUCT vía chat, Véktor como fuente de verdad, política null/NaN con imputación por mediana, ingesta interactiva con columnas riesgosas |
| 15 | ✅ | **Agent Teams (Stages 1–5d):** split AgentCash → AgentIncome + AgentExpense; AgentGoogle absorbe Calendar + Sync; intents rioplatense; `AgentTeamPlan`/`AgentTask`; `team_plan_builder.py`. **Stage 3:** `TeamPlanExecutor` DAG (skip downstream, context N→N+1), `synthesis.py`, `/confirm/group`. **Stage 4:** `GoogleToolBroker`, 3 ActionTypes Google. **Stage 5d:** AgentCalendar + AgentSync eliminados del registry. |
| 16 | ✅ | **Stage 5a:** AgentHealth v2 (fórmula 5 dims, `sub_collector/calculator/narrator`, `scorer.py` shim, `health_config_service`, PDF/DOCX export, `score_growth`, margen configurable). **Stage 5b:** AgentHelper (`docs/vektor_user_manual.yaml` + `help_documentation_service.py`, `/agent/help/chat`, `redirect_to` bidireccional, frontend `/help`). **Stage 5c:** retry externo en `TeamPlanExecutor`, tests regresión. PyYAML a deps. |
| 17 | ✅ | **Intents analíticos + tolerancia al lenguaje natural:** 8 familias, 7 ActionTypes `ANALYZE_*` + `SIMULATE_SCENARIO` (read-only). Dos capas de rescate: `DataIntentExtractor` + `intent_rescue.py` (scoring semántico + `rapidfuzz`, voseo/tildes). Math determinística en `shared/analytics.py`. `rapidfuzz` a deps. |
| 18 | ✅ | **AgentChat + migración a Sonnet 4.6 + NLP rioplatense** (`a230009e`): nuevo `agent_chat` (síntesis de respuesta, reemplaza `_generate_rich_response`). Todos los agentes Haiku 4.5 → `claude-sonnet-4-6`. `INTENT_CATALOG` `list`→`dict{desc,triggers}`, system prompt dinámico. `nlp_preprocessor.py` (lunfardo + spacy opcional). 650 tests. |
| 19 | ✅ | **Consolidación de intents 60→28 (granularidad a entidades):** variantes analíticas colapsan a 8 familias; sub-análisis → entidad `analysis_type`. `_ANALYTIC_FAMILIES` + `_resolve_legacy_discriminator()` traduce a `_intent` legacy (handlers intactos); `_LEGACY_INTENT_ALIASES` red de seguridad. `rescue_intent()` → `tuple[intent, entities]`. 699 tests, cob. 66.77%. |
| 20 | ✅ | **Filtros temporales + horarios + cierre de caja** (`88d26b73`): **F1** `lib/period.ts` (`PeriodValue` preset/year/month/week/day) + `PeriodFilter.tsx` + `GET /sales\|/expenses/date-range`. **F2** `BusinessProfile.work_days/work_open_hour/work_close_hour` (`20260616_0001`), `WorkScheduleService`, `GET/PATCH /settings/work-schedule`, onboarding Step2 + `WorkSchedulePanel`. **F3** modelo `CashClose` (`20260617_0001`, unique tenant+fecha), `CashCloseService` con **neteo de efectivo** server-side, `GET /cash-closes/preview` + `POST` (409 doble cierre) + `CashCloseModal`. TZ ART hardcoded. |
| 21 | ✅ | **Mapeo inteligente de columnas + fix rescate de adjuntos** (`beb48984`): tabla `tenant_column_mappings` (`20260620_0001`) + `ColumnMappingService` (3 capas: historial tenant → heurística → fuzzy), `GET /ingestion/files/{id}/column-mappings`, `GET\|DELETE /ingestion/column-mappings`. `confirm_file` valida requeridos (422) + crea custom fields idempotente + aprende post-confirm. Frontend `ColumnMapperPanel`. Fix: rescate corre con `pedir_aclaracion_sobre_archivo` + archivos `inferred_type="general"`. Fixes: `make migrate-neon`, JSONB `sa.text()`, confirm visible. |
| 22 | ✅ | **Mapeo universal por contexto + tablas dinámicas + time-series:** (`00b90ef5`) `mapping_contexts` + marcador `__context__` → mapeo por hoja/grupo en xlsx multi-hoja, csv, txt/pdf/docx/pptx/ocr (`_build_text_contexts`); `ColumnMapping.context_id/entity_type`; `ColumnMapperPanel` con `MultiContextMapper`. Tablas dinámicas desde field-definitions (`lib/customFields.ts`, columnas custom en `/sales /products /expenses`). Fixes NaN (amount como número), compras de mercadería → stock. (`c39b5705`) **time-series:** `transaction_date` DATE→DATETIME + `products.acquired_at` (`20260625_0001`); agregación diaria vía `func.date()`; `lib/datetime.ts`, granularidad "Hoy (horas)". 784 tests. |

### Cadena de migraciones (recientes)

- `20260401_0003` `business_heuristic_overrides`/`agent_conversation_context`/`heuristics_version` · `20260406_0001/0002` no-op stubs · `20260421_0001/0002` memoria (`business_memory`, `agent_memory`).
- `20260424_0001` `google_mcp_connections` (ORM `GoogleMcpConnection`) · `20260424_0002` `google_oauth_tokens` (solo MCP server) · `20260427_0001` `analytics_events` (ORM `AnalyticsEvent`).
- `20260429_0001` `tokens_*` en `decision_audit_log` + índice · `20260430_0001` índice `is_active` en `products` · `20260430_0002` `password_reset_tokens`.
- `20260503_0001` `provenance VARCHAR(10)` en ventas/gastos · `20260503_0002` `chat_session_log` (ORM `ChatSessionLog`).
- `20260508_0001` custom fields (`custom_fields JSONB` en 4 tablas + `vertical_field_definitions` + `tenant_custom_field_definitions` + `tenant_field_change_log`).
- `20260510_0001`/`20260511_0001` soft delete (`voided_at`, `void_reason`) en ventas/gastos/productos + `data_repair_runs`/`data_repair_items`.
- `20260512_0001` `low_stock_threshold_units` nullable (NULL=no configurado, 0=umbral sin alerta) · `20260601_0001` `approval_group_id`/`group_execution_status` en `pending_actions`.
- `20260615_0001` `score_growth INTEGER NULL` (NULL=v1, NOT NULL=v2 5 dims) · `20260616_0001` `work_days`/`work_open_hour`/`work_close_hour` en `business_profiles`.
- `20260617_0001` `cash_closes` (ORM `CashClose`, unique `(tenant_id, fecha)`) · `20260620_0001` `tenant_column_mappings` (ORM `TenantColumnMapping`).
- `20260625_0001` `transaction_date` DATE→TIMESTAMP en ventas/gastos + `products.acquired_at`.
- `20260701_0001` (FASE 0, en branch) `pipeline_events` (traza de ingestión) + `trace_id`/`content_hash`/`deleted_at` en `uploaded_files`.

**Email:** SMTP→Resend HTTP API (`app/integrations/smtp.py` usa `httpx`; Railway bloquea port 587). Variables: `RESEND_API_KEY=re_...` + `SMTP_FROM_EMAIL=noreply@vektor.app` (`SMTP_PASSWORD` alias legacy).

---

## Reglas de trabajo

- **Mostrar plan antes de escribir código y esperar confirmación.**
- Tipos estrictos (`mypy strict=true`). Cada endpoint necesita schema Pydantic.
- `tenant_id` del JWT en CADA query de negocio — nunca del cliente.
- Scores recalculan solo ante cambios de datos (Celery async). Toda decisión → `decision_audit_log` (insert-only). Fail-closed en writes sensibles.
- `ActionType` cerrado (27 valores) — cambiar requiere actualizar `RiskEngine` y tests.
- System prompts: heurísticas como valores numéricos, nunca texto narrativo. Todo input de usuario a LLM pasa por `wrap_user_input()`. Toda aritmética financiera va por `DeterministicFinance` (LLMs nunca calculan montos).
- Custom fields no se validan en write time (MVP); agregar en `field_definition_service.validate_custom_fields()` cuando haga falta.
- **No-invention rule:** ningún componente del dashboard, agente LLM, ni job de background puede mostrar análisis, scores, narrativas, alertas o conclusiones cuando `confidence_level == "LOW"` (`data_completeness_score < 50`). La UI muestra un empty state solicitando los datos faltantes. Los jobs no persisten `Insight`, `ActionSuggestion` ni notificaciones analíticas con `confidence_level == "LOW"`. Nunca reemplazar scores `None`/`0` con defaults neutrales (`or 70`, `or 50`, etc.): si falta un componente, el score real es 0 o ausente — bajar la confianza, no maquillarlo.

---

## Tests

pytest + pytest-asyncio (`asyncio_mode = "auto"`), DB en memoria SQLite + aiosqlite. Cobertura **50%** local / **60%** CI. Ej: `pytest app/tests/domain/test_health_score.py -v --no-cov`.

## CI

- `ci-backend.yml`: ruff + mypy + pytest (cov ≥ 60%) + Docker build (`backend/**` → `main/develop`).
- `ci-frontend.yml`: tsc + ESLint + `next build` (`frontend/**` → `main/develop`).

## Deploy (Railway + Vercel — beta)

Beta: único servicio Railway (sin worker/beat, Celery pausado). `vektor-api` (`backend/railway.toml`, `sh scripts/start_web.sh` → uvicorn) + Postgres Neon (externo) + Redis Railway (opcional) + Frontend Vercel (`frontend/`, `next start`). Alembic no corre al arrancar — migrations manuales contra Neon.

**Graceful bootstrap:** `app/bootstrap.py` captura errores DB/Redis → uvicorn arranca igual. `/health` liveness siempre 200 (`healthcheckPath`, timeout 120s); `/ready` chequea DB+Redis (503 si falla, no usado por Railway). **Separar worker/beat** con tráfico: restaurar `backend/worker/` + `backend/beat/`.

## Demo

`http://localhost:3000/demo` (password `Demo1234!`): `demo.kiosco@vektor.app` (Kiosco, score 74), `demo.limpieza@vektor.app` (Limpieza, 51), `demo.deco@vektor.app` (Decoración, 62). 8 semanas historial, 30 días transacciones, 8–15 productos. Regenerar: `make reset-demo`.
