# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

---

## Proyecto

Véktor es una plataforma SaaS de salud financiera para PYMEs argentinas (kioscos, decoración hogar, limpieza). Multi-tenant, monolito modular en v1.

### Estado actual importante

- El chat productivo entra por `ChatOrchestrator`; no despacha directo desde el router al sub-agente.
- Los clientes Anthropic se construyen vía `app/integrations/anthropic_client.py`; no instanciar `anthropic.AsyncAnthropic()` directo en agentes.
- Integraciones Google son **MCP-based** (`ENABLE_GOOGLE_MCP_TOOLS` + `MCP_SERVER_URL`). `Google Login` social vía `/auth/oauth/google/*` es independiente — no confundir.
- Frontend `/apps`: conexión Google, estado de integraciones, reconnect cuando una acción devuelve `REQUIRES_RECONNECT`.
- Pipeline de archivos centralizado en `app/application/services/file_parsing.py`. Uploads de chat: parseo síncrono. Ingestión: pipeline propio con confirmación humana.
- Cadena Alembic incluye `20260401_0003_restore_chat_context_and_heuristics.py` y stubs `20260406_0001/0002_stub.py`.
- Frontend: tema dark unificado (`vektor-night`, `vektor-ink`, `vektor-surface`, `vektor-border`), tipografías `Barlow Condensed` + `Inter`.
- Landing pública (`/`): hero full-screen, social proof, highlights, preview de dashboard.
- Dashboard dividido: `/dashboard` (health score + KPIs) y `/dashboard/analisis` (charts + insights), navegación launchpad compartida.
- Ticker económico alimentado por `GET /api/economia`. Insights de charts: `GET /insights/current` (template-based). El stub `GET /api/analisis/insight` ya no se usa.
- Benchmarks de margen desde JSON (`heuristics/verticals/loader.py → load_margin_benchmark()`). El health engine no importa los .py de vertical. Alias `kiosco` → `kiosco_almacen` normalizado en el loader.
- `HealthAlertBanner.tsx`: alertas fixed bottom-right cuando `score < 75` y `risk_code` activo (CASH_LOW, MARGIN_LOW, STOCK_CRITICAL, SUPPLIER_DEPENDENCY). Dismissable, 800ms delay.
- `SmartTable<T>` (`src/components/ui/SmartTable.tsx`): selector de columnas + exportación CSV UTF-8 BOM. Columnas con `defaultVisible` y `csvValue`.
- `GET /insights/breakdown?days=N`: gastos por categoría, top 5 proveedores, stock crítico. Alimenta dos panels en `DashboardAnalysisScreen`.
- `GET /forecast/cash`: 3 tiers según historial (Tier 1 <30d promedio 14d, Tier 2 30–90d EWMA, Tier 3 90d+ patrón semanal). Redis TTL 6h; `?refresh=true` fuerza recálculo.
- `analytics_events`: log insert-only anonimizado por vertical (sin `tenant_id`). `AnalyticsRepository.compute_margin_benchmark()` usa `percentile_cont` con ≥5 muestras/90 días para benchmarks data-driven. `GET /admin/analytics/benchmarks` (SUPERADMIN).
- `decision_audit_log`: columnas `tokens_input/output/total` (INTEGER DEFAULT 0) + campos en `decision_data`: `ceo_target_agent`, `sub_agent_name`, `token_calls`. Migración: `20260429_0001`.
- **DeterministicFinance** (`app/application/services/deterministic_finance.py`): único lugar para aritmética financiera. LLMs nunca calculan números — siempre delegar a este servicio.
- **ValidationGate** (`app/application/services/validation_gate.py`): rechaza `confidence=LOW` antes de que el agente procese datos. Sprint 11.
- **Provenance:** columna `provenance VARCHAR(10)` en `sales_entries`/`expense_entries` (migración `20260503_0001`). Demo data marcada como `"demo"`; real data como `"manual"` o `"import"`.
- **ChatMemoryService** (`app/application/services/chat_memory_service.py`): 4ª capa de memoria — log durable de eventos por sesión (`chat_session_log`). Entry types: `DATA_LOADED`, `DATA_REJECTED`, `FILE_UPLOADED`, `INTENT_DETECTED`, `QUERY_ANSWERED`. Sprint 11.
- **Custom Fields** (Sprint 12): columna `custom_fields JSONB` en `sales_entries`, `expense_entries`, `products`, `business_profiles`. Definiciones base en `vertical_field_definitions`; overrides por tenant en `tenant_custom_field_definitions`; undo log en `tenant_field_change_log`. API: `/fields`. Gestión en frontend: `FieldDefinitionsPanel.tsx` + `SchemaERDView.tsx` en `/settings`.
- **Agent Automation Rules** (`app/application/services/automation_service.py`): reglas de consentimiento explícito para auto-ejecutar acciones recurrentes. Feature flag: `ENABLE_AGENT_AUTOMATIONS`. API: `/automations`. Sprint 11.
- **Soft delete auditado** (Sprint 13): columnas `voided_at` + `void_reason VARCHAR(30)` en `sales_entries` y `expense_entries`. `void_reason` acepta: `REPAIR_MISCLASSIFIED_IMPORT`, `USER_REQUEST`, `DATA_QUALITY`. Migraciones: `20260510_0001` + `20260511_0001`.
- **DataRepairService** (`app/application/services/data_repair_service.py`): detecta `SaleEntry` creadas desde CSVs de productos mal clasificados, anula las ventas (soft delete) y reconstruye los `Product`. Flujo: `dry_run=True` → preview → `apply`. Trazabilidad en `data_repair_runs` + `data_repair_items`. API admin: `POST /admin/repairs/misclassified-product-imports/dry-run` + `.../apply` (SUPERADMIN).
- **Product lookup en AgentCash** (Sprint 13): `_lookup_product_price()` hace fuzzy match contra catálogo del tenant. Si el producto es ambiguo, el agente pregunta al usuario antes de registrar. `product_id` se inyecta en la entidad y el PATCH de ventas lo acepta y recalcula el monto.
- **Estados canónicos de producto** (Sprint 14): `ProductResponse.stock_status` computed field — `"in_stock" | "low_stock" | "out_of_stock"`. Frontend usa estos valores en badges ("En stock" / "Pocas unidades" / "Sin stock") y filtros URL (aliases `ok/low/out` siguen funcionando). Chip "En camino" es stub en dashboard (count=0, sin lógica hasta `purchase_orders`).
- **UPDATE_PRODUCT vía chat** (Sprint 14): `ActionType.UPDATE_PRODUCT` (MEDIUM). AgentCEO intent `update_product` → AgentStock `PRODUCT_UPDATE` handler. Extrae campos a modificar; distingue `sku` (identificador) de `new_sku` (campo a actualizar). `PendingActionService` ejecuta via `ProductRepository`; recalcula score solo si cambian `sale_price_ars/unit_cost_ars/stock_units`. `ChatOrchestrator` eliminó la restricción "no puede modificar datos vía chat"; incluye regla FUENTE DE VERDAD: Google Sheets solo para import/export, nunca para modificar.
- **Política null/NaN en imports** (Sprint 14): helpers en `file_parsing.py` — `normalize_numeric()`, `normalize_categorical()`, `impute_column()` (quantity→0, numeric→mediana, categorical→None), `compute_column_null_stats()`, `flag_columns_at_risk()` (umbral 35%). `_parse_spreadsheet` calcula `columns_at_risk` y lo incluye en `parsed_summary_json`. Validators `amount_no_nan` en `CreateSaleRequest` y `CreateExpenseRequest`.
- **Ingesta interactiva para columnas riesgosas** (Sprint 14): `GET /ingestion/files/{id}/preview` expone `columns_at_risk[]`. `POST /files/{id}/drop-columns` elimina columnas de todos los buckets del summary (ventas/gastos/productos/stock_detectado). `POST /files/{id}/cancel` marca el archivo como `NEEDS_COMPLETION`. Frontend `FileListSection` muestra panel por cada columna >35% nulos con botones "Eliminar columna" y "Cancelar y completar datos".
- **Horarios laborales + cierre de caja** (Sprint 20): `work_schedule_service.py` (BusinessProfile + onboarding + settings, migración `20260616_0001`) y `cash_close_service.py` con arqueo y neteo de efectivo (`cash_closes`, migración `20260617_0001`). Router `/cash-closes`: `GET /preview`, `POST` (crear cierre), `GET` (listar). Filtros temporales jerárquicos en ventas/gastos.
- **Mapeo inteligente de columnas** (Sprint 21): `column_mapping_service.py` (`ColumnMapper`) con aliases por tenant en `tenant_column_mappings` (migración `20260620_0001`). Frontend: `ColumnMapperPanel.tsx`. Rescate de adjuntos en chat: Redis `pending_file:{conversation_id}` TTL 30min.
- **Mapping contexts universales:** `parsed_summary_json` incluye `mapping_contexts[]` (mapeo por hoja/grupo en todos los formatos — spreadsheet, texto, imagen) + tablas dinámicas desde field-definitions.
- **`transaction_date` es DATETIME** (migración `20260625_0001`, también `products.acquired_at`): soporta intradía. En queries de rango/agregación por día usar `func.date()`.
- **Remediación de ingestión (FASES 0–3, PR #4):** FASE 1 fixes — CSV sin límite de filas (eliminado `[:50]`), detección de delimitador `;`, hojas no clasificables preservadas. FASE 2 — `llm_column_mapper.py` (4ª capa de mapeo LLM), `llm_file_type_detector.py` (detección de tipo por contenido), traza en `pipeline_events` (`pipeline_event_service.py`, migración `20260701_0001`), UI de clarificación inline. FASE 3 — `expense_entries.product_id` (mig `20260705_0001`), productos `requires_completion` (mig `20260705_0002`), sync `InventoryBalance` (`inventory_balances`).
- **Resumen económico analítico** (FASE 4): `GET /economic-summary` (`economic_summary_service.py`) + frontend `/resumen-economico`. Stock valuado = `stock_units × unit_cost_ars`; `missing_cost` solo con stock>0; doble disclaimer (NO es informe contable). Sin migraciones.
- **Parser Libro Diario** (`file_parsing.py`): `detect_libro_diario_header()` + `parse_libro_diario()` — doble encabezado Dinero/Mercadería × Ingreso/Egreso. Las hojas derivadas del Libro Diario no se importan además (duplicarían ventas). `PaymentMethod.ACCOUNT` + normalización de pagos canónicos en ventas importadas.
- **Categorías de gasto canónicas + COGS/OPEX:** catálogo de 13 categorías en `app/domain/expense_categories.py`; `infer_expense_type()` discrimina `COGS` (product_id vinculado o categoría `INVENTORY`) vs `OPEX` (migración `20260710_0001`). Gastos de mercadería del vertical → INVENTORY/COGS.
- **Sección "Otros"** (`unclassified_records`, migración `20260712_0001`): filas ambiguas de imports ya NO caen a ventas por default — van a `unclassified_records` con `suggested_entity`. Router `/others`: listar, `GET /count`, clasificar, importación masiva de sugeridos. Frontend `/otros` con badge de sugerencia.
- **Diagnóstico MCP Google** (FASE 5): `GET /integrations/google/diagnostics` (SUPERADMIN) vía `mcp_diagnostics_service.py` — chequea flag/URL/secret/conectividad/auth. Runbook: `docs/runbooks/google_mcp_oauth.md`.

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

### Backend (correr desde `backend/`)

```bash
make dev                    # Docker Compose con hot reload
make dev-bg / make stop / make logs / make shell

# Tests
make test                   # pytest con cobertura (mínimo 50%)
make test-fast / make test-watch
make test-file FILE=app/tests/api/v1/test_auth.py
pytest app/tests/api/v1/test_auth.py::test_login -v

# Calidad
make lint / make format / make typecheck / make check

# Migraciones (dir: backend/app/persistence/migrations/versions/, NO backend/alembic/versions/)
make migrate / make migrate-down   # contra Docker Postgres local
make migrate-neon                  # contra Neon (producción) — pide DATABASE_URL del shell
make migrate-create MSG="descripcion"
make migrate-history

# Demo
make seed-demo / make reset-demo
make db-reset               # ⚠️ PELIGROSO
```

### Scripts operativos (`backend/scripts/`)

- `diag_account.py` / `diag_expenses.py` — diagnóstico de cuentas reales: **read-only, solo SELECT, nunca imprimen la connection URL**. La `DATABASE_URL` la provee el usuario desde su shell (el `.env` está bloqueado para lectura).
- `void_misclassified_imports.py` — anula (soft delete auditado) registros importados mal clasificados.
- `backfill_inventory_balances.py` / `backfill_expense_categories.py` — backfills vertical-aware.
- `reanalyze_uploaded_files.py` — re-corre el pipeline de parsing sobre archivos ya subidos (`--include-imported` disponible).
- `_db.py` — helper compartido de conexión para los scripts.

### Frontend (correr desde `frontend/`)

```bash
npm run dev / npm run build / npm run lint / npm run type-check / npm run test
```

### Variables de entorno

- Backend: `backend/.env.example` → `backend/.env`
- Frontend: `frontend/.env.local.example` → `frontend/.env.local` (`NEXT_PUBLIC_API_URL`)
- En producción: Railway/Vercel, nunca commitear.

---

## Arquitectura del backend

### Flujo de datos principal

```
HTTP Request
  → deps.py (JWT decode + tenant_id injection)
  → Router (api/v1/)
  → Application Service
  → Business State Layer (BSL)  ← agrega 30 días de transacciones
  → Health Engine (domain/health_score.py)  ← calcula score compuesto
  → Persistence (repository)
  → decision_audit_log (insert-only, siempre)
  → Celery task (score recalculation async, post-write)
```

**Regla crítica:** Datos crudos NUNCA llegan al Health Engine directamente. Todo pasa por `BusinessStateLayer.compute()` → `BusinessState` con 5 scores (0–100): `liquidity`, `profitability`, `cost_control`, `sales_momentum`, `debt_coverage`.

### Capas y responsabilidades

| Capa | Path | Responsabilidad |
|------|------|-----------------|
| API | `app/api/v1/` | Routing, validación Pydantic, auth deps |
| Deps | `app/api/v1/deps.py` | JWT decode, `get_current_user`, `get_current_tenant`, `require_role()` |
| Application | `app/application/services/` | Orquestación: auth, cash, cash_close, conversation, google_oauth, health_score, health_config, onboarding, pending_action, score_trigger, stock, supplier, business_memory, agent_memory, forecast, analytics, deterministic_finance, validation_gate, chat_memory, field_definition, automation, data_intent_extractor, ingestion_import, data_repair, report_export, team_plan_executor, help_documentation, column_mapping, llm_column_mapper, llm_file_type_detector, pipeline_event, economic_summary, work_schedule, mcp_diagnostics |
| Commands/Queries | `app/application/commands/` `app/application/queries/` | CQRS writes/reads |
| DTOs | `app/application/dto/` | Objetos de transferencia entre capas |
| DB middleware | `app/application/db/tenant_context.py` | Inyecta tenant_id en SQLAlchemy |
| **Agents** | `app/application/agents/` | Capa multiagente LLM |
| Domain | `app/domain/` | Entidades puras Python |
| BSL | `app/state/business_state_layer.py` | Agrega revenue/expenses 30 días → 5 scores |
| Heuristics | `app/heuristics/` | Reglas por vertical |
| Persistence | `app/persistence/` | SQLAlchemy async, repositories, Alembic |
| Jobs | `app/jobs/` | Celery: scores, notifications, reports, ingestion |
| Security | `app/application/security/` | `prompt_defense.py` (`wrap_user_input()`) |

### API Routers (`app/api/v1/`)

Registrados en `router.py`. Dominios: `auth`, `oauth`, `tenants`, `users`, `business_profiles`, `sales`, `expenses`, `products`, `health_scores`, `insights`, `momentum`, `notifications`, `files`, `ingestion`, `onboarding`, `agent`, `integrations`, `forecast`, `admin`, `fields`, `automations`, `settings`, `cash_closes` (`/cash-closes`), `others` (`/others`), `economic_summary` (`/economic-summary`).

**Router `settings`** (`app/api/v1/settings.py`):
- `GET /settings/health-config` — configuración de margen actual del tenant
- `PATCH /settings/health-config` — actualizar objetivos (OWNER/ADMIN); valida `target_margin_pct >= warning_margin_pct`, rango [0.0, 80.0]
- `DELETE /settings/health-config` — resetear a valores del vertical

**`POST /health-scores/{snapshot_id}/export`** — genera informe PDF o DOCX vía `report_export_service.py`. Body: `{format: "pdf"|"docx", narrative: str}`.

### Autenticación y multi-tenancy

- JWT (HS256, python-jose). Payload: `sub` (user_id), `tenant_id`, `role_code`.
- `get_current_tenant_id` se inyecta en TODOS los endpoints de negocio. El `tenant_id` viene del JWT — nunca del body/path.
- Roles: `OWNER`, `ADMIN`, `VIEWER`. Se aplica con `require_role("OWNER", "ADMIN")`.
- En producción: `/docs`, `/redoc` y `/openapi.json` están deshabilitados.

### Celery

Queues: `default`, `scores`, `notifications`, `reports`, `ingestion`.
Post-write: `trigger_score_recalculation.delay(str(tenant_id), triggered_by="...")`.
Beat schedule: momentum update + weekly email (lunes 08:00 ART).

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

- Insights **template-based**, no LLM. Templates en `app/heuristics/insight_templates.py`.
- Risk codes: `CASH_LOW`, `MARGIN_LOW`, `STOCK_CRITICAL`, `SUPPLIER_DEPENDENCY`.
- Benchmarks canónicos: `app/application/data/heuristics/{kiosco_almacen,limpieza,decoracion_hogar}.json` (campos: `critical_below`, `warning_below`, `healthy_min`, `healthy_max`). Con ≥5 muestras/90 días, `HealthScoreService` usa el benchmark data-driven.

### Capa de Agentes LLM (`app/application/agents/`)

8 sub-agentes coordinados por AgentCEO + AgentChat (capa de respuesta). El cliente NUNCA elige el agente destino. En el registry hay 9 entradas (`agent_cash` es alias de `agent_income`).

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

**Auto-ejecución:** `REGISTER_EXPENSE` sin confirmación (`_AUTO_EXECUTE_ACTION_TYPES` en `api/v1/agent.py`). Prefijo "Gasto registrado.".

**Deduplicación por fingerprint** (`operation_fingerprints`): SHA-256 de `{tenant_id}:{action_type}:{amount}:{date}` para `REGISTER_SALE/EXPENSE/PURCHASE`. Duplicado → `execution_status: "DUPLICATE"`.

**Rate limit:** 50 mensajes/día por tenant. Redis key: `rate:chat:{tenant_id}:{date}`. Compartido entre `/chat` y `/chat/stream`.

**RiskEngine** (`shared/risk_engine.py`) — determinístico, sin LLM. `HIGH`/`MEDIUM` requieren aprobación; `LOW` no.

**Retry** (`POST /agent/retry/{pending_id}`): solo `status=APPROVED`, `execution_status in (FAILED, REQUIRES_RECONNECT)`, `is_external=True`, máximo 1 reintento.

**`execution_status`:** `IN_PROGRESS` → `SUCCEEDED | FAILED | REQUIRES_RECONNECT`. `REQUIRES_RECONNECT` = `McpToolAuthError`; frontend guía a `/apps`.

**ContextBuilder** (`shared/context_builder.py`) — prioridades de contexto (en orden):
1. `historical_data` (400t) → `conversation_history` (1000t) → `recent_events` (800t) → `current_snapshot` (600t, siempre) → `uploaded_files` (400t) → `agent_memory` (300t) → `business_heuristics` (300t, siempre) → `intent_and_entities` (200t, siempre)

**HeuristicEngine** (`shared/heuristic_engine.py`): `get(business_type)` síncrono; `get_async(...)` aplica `BusinessHeuristicOverride` de la DB. `to_prompt_fragment()` genera valores numéricos para system prompts — nunca texto narrativo. Fallback a `kiosco_almacen`.

**AgentCEO — flujo:** `nlp_preprocessor` (Sprint 18, `agents/shared/nlp_preprocessor.py`) normaliza lunfardo/rioplatense (merca, birra, remarcar, guita, etc.) antes del LLM; spacy (`es_core_news_sm`) opcional con fallback a regex puro; las entidades NLP se inyectan como anotación pre-análisis en el prompt del CEO. Luego `classify_intent()` → LLM (max_tokens=800) → **28 intents** del `INTENT_CATALOG` en español rioplatense (incluye `intent_desconocido`). `INTENT_CATALOG` es `dict[str, {desc, triggers}]` y el CEO construye el system prompt dinámicamente desde el catálogo. **Sprint 19 (consolidación):** las 8 familias analíticas son UN intent cada una; el sub-análisis va en la entidad `analysis_type` (best-effort). `build_plan()` traduce `(intent, analysis_type)` → el `_intent` legacy que el handler espera vía `_resolve_legacy_discriminator()` (default por familia si falta `analysis_type`); el mapeo vive en `_ANALYTIC_FAMILIES`. `_LEGACY_INTENT_ALIASES` resuelve keys granulares en vuelo (red de seguridad). Luego `INTENT_TO_ACTION_TYPE` + `INTENT_TO_AGENT` (determinísticos, en `ceo/team_plan_builder.py`) → `build_plan()` → `AgentTeamPlan` → `registry.get_sub_agent(name)`. Intent `actualizar_producto` → `agent_stock` → `UPDATE_PRODUCT` action.

**Rescate de intent (Sprint 17):** cuando el CEO devuelve `intent_desconocido`, ChatOrchestrator NO corta inmediatamente. Aplica dos capas determinísticas (sin LLM) en `_rescue_unknown_intent()`: (1) `DataIntentExtractor` sobre los adjuntos parseados → si hay datos importables, mapea a `analizar_precios`(`analysis_type=lista`)/`analizar_archivo`; (2) `intent_rescue.rescue_intent()` (en `shared/intent_rescue.py`, devuelve `tuple[intent, entities]` con `analysis_type`) — scoring semántico de verbo ambiguo + objeto de negocio + tipo de adjunto, con normalización de voseo/tildes y fuzzy matching (`rapidfuzz`, umbral 85) para typos. Solo si ambas fallan corta: `out_of_scope` (off-topic claro → mensaje de scope) o `pedir_aclaracion_negocio`/`pedir_aclaracion_sobre_archivo` (suena a negocio pero ambiguo → pide detalle). Constantes `_NO_AGENT_INTENTS`/`_NO_AGENT_MESSAGES` en `chat_orchestrator.py`. Para los 7 ActionTypes analíticos (+`ANSWER_HELP_REQUEST`), `build_plan()` inyecta el sub-análisis en `entities["_intent"]` (resuelto desde `analysis_type`) para que el handler distinga sub-análisis dentro de un mismo ActionType.

**Handlers analíticos (Sprint 17):** read-only, LOW risk, sin aprobación. Math determinística centralizada en `shared/analytics.py` (funciones puras: márgenes, días de stock, sobrestock, estrella/problemático, anomalías de gasto, punto de equilibrio). Los agentes cargan datos vía repos y arman un `message` determinístico — los LLM NUNCA calculan. Familias: AgentStock (`ANALYZE_PRICES`, `ANALYZE_STOCK_DATA`), AgentIncome (`ANALYZE_FILE`, `ANALYZE_SALES_DATA` + clientes stub), AgentExpense (`ANALYZE_EXPENSE_DATA`), AgentSupplier (`ANALYZE_SUPPLIER_DATA`), AgentHealth (`SIMULATE_SCENARIO`: proyección de caja vía ForecastService + what-if vía DeterministicFinance). Queries nuevas: `product_repository.get_products_with_margin`, `sale_repository.get_daily_velocity`/`get_sales_by_product`, `expense_repository.get_expense_stats_by_category`.

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

### Sistema de Memoria (cuatro capas)

| Capa | Servicio | TTL | Qué almacena |
|------|----------|-----|--------------|
| Conversacional | `ConversationService` | 24h Redis + PG | Últimos 10 turnos |
| Negocio | `BusinessMemoryService` | 5min | Resumen financiero (ventas, gastos, alertas) |
| Agente | `AgentMemoryService` | 5min | Patrones: método de pago, monto promedio (Welford), top ActionTypes |
| Sesión chat | `ChatMemoryService` | permanente PG | Eventos de carga de datos (DATA_LOADED, DATA_REJECTED, FILE_UPLOADED, INTENT_DETECTED, QUERY_ANSWERED) |

`AgentMemoryService.record_action()` post-acción confirmada (fail-silent). Confianza: `min(1.0, 0.5 + count/20)`.
**Tabla:** `agent_memory` — `(tenant_id, key)` unique, `value` JSONB, `occurrence_count`, `confidence`.
**`ChatMemoryService`**: escribe en `chat_session_log` (migración `20260503_0002`). Permite saber qué se cargó en sesiones previas sin depender del historial conversacional.

### Integración MCP Google

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

### Archivos e ingestión

**`file_parsing.py`** centraliza: sanitización, detección MIME, `parsed_summary_json`, helpers chat (`summary_columns`, `summary_row_count`, `preview_value_from_summary`).

**Chat upload** (`POST /files/upload?purpose=chat`): parseo síncrono → `UploadedFile(processing_status=DONE)`. Fallo → 422, sin persistir.

**Ingestión** (`api/v1/ingestion.py` + `jobs/ingestion_worker.py`): Upload → Parse async (Celery) → Preview → Confirm (humano obligatorio).

### Observabilidad

- `structlog`: `from app.observability.logger import get_logger` → `get_logger(__name__)`.
- `bind_request_context(tenant_id, user_id)` en `deps.py` por request.
- Rate limiting: `slowapi` (200 req/min).

---

## Arquitectura del frontend

- Next.js 15 App Router. Protegidas: `src/app/(protected)/`. Públicas: `src/app/(public)/`.
- Estado global: Zustand (`src/stores/`). Server-state: TanStack Query.
- HTTP: axios wrapper `src/lib/api.ts`. UI: Tailwind CSS + `src/components/ui/`. Charts: Recharts. Forms: Zod.

### Estado visual

- Tema dark: `src/styles/globals.css` + `tailwind.config.ts` con tokens `vektor-*`.
- Tipografías en `src/app/layout.tsx`: `Barlow Condensed` (UI) + `Inter` (texto).
- `EconomicTicker` renderizado desde `(protected)/layout.tsx` solo en rutas dashboard; mobile → botón/modal.
- Dashboard: `DashboardLaunchpadNav` tabs/dots compartidos entre `/dashboard` y `/dashboard/analisis`.

### Organización del frontend

| Directorio | Responsabilidad |
|------------|-----------------|
| `src/features/` | auth, chat, dashboard, onboarding, ingestion, notifications |
| `src/services/` | HTTP por dominio: auth, sales, expenses, products, health_score, dashboard, momentum, notifications, ingestion, onboarding, files, agent |
| `src/stores/` | `authStore` (JWT + user), `toastStore` |
| `src/types/api.ts` | Tipos TypeScript de respuestas API |
| `src/components/auth/AuthHydrationBoundary.tsx` | Hidrata auth desde localStorage |

### Rutas protegidas (`src/app/(protected)/`)

| Ruta | Descripción |
|------|-------------|
| `/chat` | **Home principal** — chat página completa |
| `/dashboard` | Launchpad 1: health score hero + KPIs |
| `/dashboard/analisis` | Launchpad 2: charts + ForecastPanel + breakdown gastos + stock crítico |
| `/sales` | Analytics + lista de ventas |
| `/expenses` | Analytics + lista de gastos |
| `/products` | Catálogo; query param `?stock=ok|low|out` |
| `/apps` | Integraciones Google |
| `/settings` | Cuenta, configuración, panel de custom fields (`FieldDefinitionsPanel` + `SchemaERDView`), config de margen (`HealthConfigPanel`) y horarios laborales |
| `/help` | Chat de ayuda de plataforma — `HelpChat.tsx`, endpoint `/agent/help/chat` |
| `/ingestion` | Pipeline de ingestión de archivos con preview/confirm |
| `/otros` | Registros sin clasificar (`unclassified_records`) — clasificación manual + importación masiva de sugeridos |
| `/resumen-economico` | Resumen económico analítico (`GET /economic-summary`) — no contable, con disclaimers |

**Ruta pública:** `/oauth/callback?session_id=` → `POST /auth/oauth/google/exchange`.

### Chat

- `/chat` = home post-login. `ChatPanel.tsx` existe pero no está en el layout global.
- `conversation_id`: UUID generado por `useChatStore.newConversation()`. No viene del servidor.
- Adjuntos: `AttachmentPicker.tsx` — hasta 3 archivos, upload inmediato a `POST /files/upload?purpose=chat`.
- `useChat` expone `send` (REST) y `sendStream` (SSE). `MUTATING_ACTIONS`: 7 types que invalidan queries.

### Google OAuth login federado

1. `POST /auth/oauth/google/start` → redirect a Google
2. `/oauth/callback?session_id=` → `POST /auth/oauth/google/exchange`
3. Si `link_required`: vincular con contraseña → `POST /auth/oauth/google/link-pending`

### Integraciones MCP (frontend)

- `/apps` gestiona conexión, reconexión y desconexión Google.
- Si falla con `REQUIRES_RECONNECT` → guía a `/apps`.
- `GOOGLE_MCP_OAUTH_REDIRECT_URI` debe estar registrado exactamente en Google Cloud Console.
- Botón conexión abre ventana en blanco y redirige cuando llega `auth_url` (evita popup blocker).

### Endpoints frontend recientes

- `GET /api/economia` — dólar oficial/blue/MEP/CCL + inflación/REM/BCRA (`revalidate=1800`)
- `GET /forecast/cash` → `fetchCashForecast()` → `CashForecastResponse/ForecastPoint`
- `GET /insights/breakdown?days=N` → `BusinessBreakdownResponse`
- `GET /health-scores/history/v2` → `HealthScoreV2Response[]`
- `GET /insights/current` → `InsightBlock` en análisis (el stub `/api/analisis/insight` ya no se usa)

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
| 15 | ✅ | **Agent Teams (Stages 1–5d completos):** AgentIncome + AgentExpense (split de AgentCash); AgentGoogle absorbe AgentCalendar + AgentSync; 17 intents rioplatense; AgentTeamPlan/AgentTask; CEO → haiku-4-5; `team_plan_builder.py`. **Stage 3:** `TeamPlanExecutor` DAG (skip downstream en fallo, context N→N+1), `synthesis.py` (sonnet-4-5), planes compuestos, `/confirm/group`. **Stage 4:** `GoogleToolBroker`, 3 ActionTypes Google (`UPLOAD_TO_DRIVE`, `CREATE_GOOGLE_DOC`, `APPEND_TO_SHEET`). **Stage 5d:** AgentCalendar + AgentSync eliminados del registry. |
| 16 | ✅ | **Stage 5a:** AgentHealth v2 — fórmula 5 dims, `sub_collector/calculator/narrator`, `scorer.py` shim, `health_config_service`, PDF/DOCX export, `score_growth` en API/schema, margen configurable en `/settings`. **Stage 5b:** AgentHelper con `docs/vektor_user_manual.yaml` + `help_documentation_service.py`, endpoint `/agent/help/chat` (sin rate limit), `redirect_to` bidireccional, frontend `/help` + `HelpChat`. **Stage 5c:** retry en `TeamPlanExecutor` para tasks externas (1 retry + backoff), structlog `agent_name` en CEO, `test_health_engine_regression.py`, `test_team_plan_executor_retry.py`, `test_help_documentation.py`. PyYAML agregado a deps. |
| 17 | ✅ | **Intents analíticos + tolerancia al lenguaje natural:** catálogo 18→60 intents en 8 familias (archivos, precios, stock, ventas, gastos, proveedores, caja, clientes-stub, fallback). 7 ActionTypes analíticos read-only (`ANALYZE_*`, `SIMULATE_SCENARIO`). Dos capas de rescate antes de cortar: `DataIntentExtractor` (adjuntos) + `intent_rescue.py` (scoring semántico + `rapidfuzz`, normalización voseo/tildes). Se conserva `out_of_scope` para off-topic claro; ambiguo de negocio → `pedir_aclaracion_negocio`/`_sobre_archivo`. Math determinística en `shared/analytics.py`. Handlers en los 5 agentes; 4 queries nuevas en repos. Clientes (4 intents) stub → Sprint 18. `rapidfuzz` agregado a deps. |
| 18 | ✅ | **AgentChat + migración a Sonnet 4.6 + NLP rioplatense** (commit `a230009e`): nuevo `agent_chat` que absorbe la síntesis de respuesta al usuario (reemplaza `_generate_rich_response` del ChatOrchestrator; `_format_agent_result`/`_render_session_memory`/`_format_history_turn` movidos a AgentChat). Todos los agentes migrados Haiku 4.5 → `claude-sonnet-4-6` (max_tokens subidos). `INTENT_CATALOG` reestructurado `list[str]` → `dict[str, {desc, triggers}]` (60 intents, triggers rioplatenses, system prompt dinámico). `nlp_preprocessor.py`: normalización de lunfardo + spacy opcional antes del CEO. Google MCP: allowlist corregida (eliminado alias `agent_cash`). Limpieza repo: `.venv/`+`.claude-flow/` sacados del índice. 650 tests passing. |
| 19 | ✅ | **Consolidación de intents 60→28 (granularidad a entidades):** las 35 variantes analíticas (mismo agente + mismo ActionType) colapsan a 8 familias; el sub-análisis se mueve a la entidad `analysis_type`. `generar_informe`→`consultar_estado_negocio`; `ayudar_con_archivo`/`explicar_que_puedo_hacer_con_datos`→`ayuda_plataforma`. Espacio de decisión del clasificador: ~60→~25 intents sin hermanos solapados. Mecanismo: `_ANALYTIC_FAMILIES` + `_resolve_legacy_discriminator()` traduce `analysis_type`→`_intent` legacy (handlers **intactos**); `_LEGACY_INTENT_ALIASES` cubre keys en vuelo. `intent_rescue.rescue_intent()` ahora devuelve `tuple[intent, entities]`. Smoke clasificador: 20/20. 699 tests passing, cobertura 66.77%. |
| 20 | ✅ | Filtros temporales jerárquicos (ventas/gastos), horarios laborales (BusinessProfile + onboarding + settings, mig `20260616_0001`), cierre de caja con arqueo + neteo de efectivo (`cash_closes`, mig `20260617_0001`). |
| 21 | ✅ | Mapeo inteligente de columnas (`tenant_column_mappings` mig `20260620_0001`, `ColumnMapper`, `ColumnMapperPanel.tsx`) + fix rescate de adjuntos en chat (Redis `pending_file:{conversation_id}` TTL 30min). |

### Post-Sprint 21 — proyectos mergeados a main

| Proyecto | Descripción |
|----------|-------------|
| Mapeo universal de imports | `mapping_contexts[]` en `parsed_summary_json` (por hoja/grupo en todos los formatos) + tablas dinámicas desde field-definitions. |
| Transaction datetime | `transaction_date` DATE→DATETIME + `products.acquired_at` (mig `20260625_0001`). Usar `func.date()` en queries de rango. |
| Remediación de ingestión F0–F3 (PR #4) | F1: CSV sin límite `[:50]`, delimitador `;`, hojas preservadas. F2: LLM 4ª capa de mapeo + detección de tipo por contenido + traza `pipeline_events` (mig `20260701_0001`) + UI clarificación inline. F3: `expense_entries.product_id` (mig `20260705_0001`), productos `requires_completion` (mig `20260705_0002`), sync `InventoryBalance`. |
| Resumen económico F4 (PR #5) | `GET /economic-summary` + frontend `/resumen-economico`. Sin migraciones. |
| Diagnóstico MCP F5 (PR #6) | `GET /integrations/google/diagnostics` (SUPERADMIN) + runbook `docs/runbooks/google_mcp_oauth.md`. El código MCP está completo; el fallo OAuth restante es operacional (Google Cloud Console). |
| Libro Diario + pagos canónicos | Parser doble encabezado en `file_parsing.py`, `PaymentMethod.ACCOUNT`, mercadería→COGS por vertical, scripts `void_misclassified_imports.py` + `backfill_inventory_balances.py`. |
| Categorías COGS + Otros | Catálogo 13 categorías (`domain/expense_categories.py`), `expense_type` COGS/OPEX (mig `20260710_0001`), `unclassified_records` + `/others` + `/otros` (mig `20260712_0001`), importación masiva de sugeridos. |

### Cadena de migraciones (recientes)

| Migración | Qué agrega |
|-----------|-----------|
| `20260401_0003` | `business_heuristic_overrides`, `agent_conversation_context`, `heuristics_version` |
| `20260406_0001/0002` | no-op stubs (continuidad Alembic) |
| `20260421_0001/0002` | tablas de memoria (`business_memory`, `agent_memory`) |
| `20260424_0001` | `google_mcp_connections` (ORM: `GoogleMcpConnection`) |
| `20260424_0002` | `google_oauth_tokens` (solo MCP server, sin ORM en backend) |
| `20260427_0001` | `analytics_events` insert-only sin tenant_id (ORM: `AnalyticsEvent`) |
| `20260429_0001` | `tokens_input/output/total` en `decision_audit_log` + índice `(tenant_id, created_at)` |
| `20260430_0001` | índice `is_active` en `products` |
| `20260430_0002` | `password_reset_tokens` |
| `20260503_0001` | columna `provenance VARCHAR(10)` en `sales_entries` + `expense_entries` |
| `20260503_0002` | `chat_session_log` (ORM: `ChatSessionLog`) — 4ª capa de memoria |
| `20260508_0001` | custom fields: `custom_fields JSONB` en 4 tablas core + `vertical_field_definitions` + `tenant_custom_field_definitions` + `tenant_field_change_log` |
| `20260510_0001` | soft delete en `sales_entries` (`voided_at`, `void_reason`) + tablas `data_repair_runs` + `data_repair_items` |
| `20260511_0001` | soft delete en `expense_entries` + `products` (`voided_at`, `void_reason`); amplía check constraint de `data_repair_items` |
| `20260512_0001` | `low_stock_threshold_units` nullable en `products` (NULL = no configurado; 0 = umbral explícito sin alerta) |
| `20260601_0001` | `approval_group_id` + `group_execution_status` en `pending_actions` (Stage 3 multi-task) |
| `20260615_0001` | `score_growth INTEGER NULL` en `health_score_snapshots` (NULL = snapshot v1 pre-Stage-5a; NOT NULL = fórmula v2 con 5 dims) |
| `20260616_0001` | horarios laborales (`work_schedule`) en business profile |
| `20260617_0001` | `cash_closes` (ORM: `CashClose`) — cierre de caja con arqueo |
| `20260620_0001` | `tenant_column_mappings` (ORM: `TenantColumnMapping`) — aliases de mapeo de columnas por tenant |
| `20260625_0001` | `transaction_date` DATE→DATETIME en ventas/gastos + `products.acquired_at` |
| `20260701_0001` | `pipeline_events` (ORM: `PipelineEvent`) + auditoría de archivos — traza del pipeline de ingestión |
| `20260705_0001` | `product_id` en `expense_entries` (vincula gasto de mercadería al producto) |
| `20260705_0002` | `requires_completion` en `products` (productos creados incompletos desde imports) |
| `20260710_0001` | `expense_type` COGS/OPEX en `expense_entries` + catálogo canónico de categorías |
| `20260712_0001` | `unclassified_records` (ORM: `UnclassifiedRecord`) — sección "Otros" |

**Post-Sprint 8–9:** Email reemplazado SMTP→Resend HTTP API (`app/integrations/smtp.py` usa `httpx`). Railway bloquea port 587. Variables Railway: `RESEND_API_KEY=re_...` + `SMTP_FROM_EMAIL=noreply@vektor.app`. `SMTP_PASSWORD` es alias legacy.

---

## Reglas de trabajo

- **Mostrar plan antes de escribir código y esperar confirmación.**
- Tipos estrictos (`mypy strict=true`). Cada endpoint necesita schema Pydantic.
- `tenant_id` del JWT en CADA query de negocio — nunca del cliente.
- Scores recalculan solo ante cambios de datos (Celery async).
- Toda decisión → `decision_audit_log` (insert-only).
- Fail-closed en writes sensibles.
- `ActionType` cerrado (27 valores) — cambiar requiere actualizar `RiskEngine` y tests.
- System prompts: heurísticas como valores numéricos, nunca texto narrativo.
- Todo input de usuario a LLM pasa por `wrap_user_input()`.
- Toda aritmética financiera va por `DeterministicFinance` — LLMs nunca calculan montos.
- Custom fields no se validan en write time (MVP). Agregar validación en `field_definition_service.validate_custom_fields()` cuando sea necesario.
- **No-invention rule:** ningún componente del dashboard, agente LLM, ni job de background puede mostrar análisis, scores, narrativas, alertas o conclusiones cuando `confidence_level == "LOW"` (`data_completeness_score < 50`). La UI muestra un empty state solicitando los datos faltantes. Los jobs no persisten `Insight`, `ActionSuggestion` ni notificaciones analíticas con `confidence_level == "LOW"`. Nunca reemplazar scores `None`/`0` con defaults neutrales (`or 70`, `or 50`, etc.): si falta un componente, el score real es 0 o ausente — bajar la confianza, no maquillarlo.

---

## Tests

- pytest + pytest-asyncio (`asyncio_mode = "auto"`). DB en memoria: SQLite + aiosqlite.
- Cobertura: **50%** local, **60%** en CI.
- `pytest app/tests/domain/test_health_score.py -v --no-cov`

## CI

- `ci-backend.yml`: ruff + mypy + pytest (cov ≥ 60%) + Docker build. Triggers `backend/**` → `main/develop`.
- `ci-frontend.yml`: tsc + ESLint + `next build`. Triggers `frontend/**` → `main/develop`.

## Deploy (Railway + Vercel — beta)

| Servicio | Manifiesto | Start |
|----------|-----------|-------|
| `vektor-api` | `backend/railway.toml` | `sh scripts/start.sh` → uvicorn |
| `vektor-worker` | `backend/worker/railway.toml` | `sh scripts/start_worker.sh` → Celery |
| `vektor-mcp` | (servicio MCP propio) | — |
| Postgres | Neon (externo) | — |
| Redis | Railway managed | — |
| Frontend | Vercel, root `frontend/` | `next start` |

Alembic no corre al arrancar — migrations manuales contra Neon (`make migrate-neon`).

**Graceful bootstrap:** `app/bootstrap.py` captura errores DB/Redis → uvicorn arranca aunque estén caídos.
- `/health` — liveness, siempre 200 (usado por `healthcheckPath` en `railway.toml`, timeout 120s).
- `/ready` — chequea DB + Redis, devuelve 503 si falla (no usado por Railway actualmente).

**Beat** todavía no está desplegado como servicio (`backend/beat/railway.toml` en repo como referencia).

## Demo

`http://localhost:3000/demo` (password `Demo1234!`):

| Email | Vertical | Score |
|-------|----------|-------|
| demo.kiosco@vektor.app | Kiosco | 74 |
| demo.limpieza@vektor.app | Limpieza | 51 |
| demo.deco@vektor.app | Decoración | 62 |

8 semanas historial, 30 días transacciones, 8–15 productos. Regenerar: `make reset-demo`.
