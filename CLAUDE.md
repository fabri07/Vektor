# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

---

## Proyecto

Véktor es una plataforma SaaS de salud financiera para PYMEs argentinas (kioscos, decoración hogar, limpieza). Multi-tenant, monolito modular en v1.

### Estado actual importante

- El chat productivo entra por `ChatOrchestrator` y no despacha directo desde el router al sub-agente.
- Los clientes Anthropic se construyen vía `app/integrations/anthropic_client.py`; no instanciar `anthropic.AsyncAnthropic()` directo en agentes.
- Las integraciones de producto con Google hoy son **MCP-based** (`ENABLE_GOOGLE_MCP_TOOLS` + `MCP_SERVER_URL`). En paralelo sigue existiendo `Google Login` para autenticación social vía `/auth/oauth/google/*`; no confundir login social con herramientas MCP.
- En frontend existe `/apps` con flujo directo de conexión Google, estado de integraciones y manejo de reconnect cuando una acción externa devuelve `REQUIRES_RECONNECT`.
- El pipeline de archivos fue recentralizado en `app/application/services/file_parsing.py`. Los uploads de chat se parsean sincrónicamente al subir; la ingestión sigue su pipeline propio con confirmación humana.
- La cadena Alembic actual incluye una migración de compatibilidad `20260401_0003_restore_chat_context_and_heuristics.py` y stubs `20260406_0001_stub.py` / `20260406_0002_stub.py` para conservar continuidad después de retirar migraciones viejas de Google.
- El frontend fue rediseñado con tema dark unificado (`vektor-night`, `vektor-ink`, `vektor-surface`, `vektor-border`) y tipografías `Barlow Condensed` + `Inter`.
- La landing pública (`/`) ya no es un placeholder: tiene hero full-screen, social proof con carrusel, highlights y preview de dashboard.
- El dashboard quedó dividido en dos pantallas: `/dashboard` (resumen con health score y KPIs) y `/dashboard/analisis` (charts + insights), con navegación compartida tipo launchpad.
- Existe un ticker económico en la parte superior del dashboard alimentado por `GET /api/economia`. Los insights de charts llaman a `GET /insights/current` (real, template-based); el stub `GET /api/analisis/insight` ya no se usa.
- Para trabajo visual con Codex está operativo el proxy local `tools/stitch-proxy/`; Stitch se usa como referencia de diseño, no como dependencia runtime de la app.
- Los benchmarks de margen se cargan desde JSON (`heuristics/verticals/loader.py → load_margin_benchmark()`), no desde un dict hardcodeado en Python. El health engine ya no importa `deco_hogar.py`, `kiosco.py` ni `limpieza.py`. Códigos alternativos (`kiosco` → `kiosco_almacen`) se normalizan en el loader.
- `HealthAlertBanner.tsx` muestra alertas accionables fixed bottom-right cuando `score < 75` y hay `risk_code` activo (CASH_LOW, MARGIN_LOW, STOCK_CRITICAL, SUPPLIER_DEPENDENCY). Dismissable, 800ms de delay, navega a la sección relevante al hacer clic.
- `SmartTable<T>` (`src/components/ui/SmartTable.tsx`) envuelve cualquier tabla con selector de columnas (dropdown checkboxes) y exportación CSV con BOM UTF-8. Las columnas tienen `defaultVisible` y `csvValue`. Ventas oculta "Cantidad"; Gastos oculta "Proveedor" y "Recurrente".
- `GET /insights/breakdown?days=N` devuelve gastos por categoría (con %), top 5 proveedores por gasto y productos con stock crítico. Alimenta dos nuevos panels en `DashboardAnalysisScreen`.
- `GET /forecast/cash` — forecast de caja en 3 tiers según historial disponible: Tier 1 (<30 días) promedio simple 14d LOW, Tier 2 (30–90d) EWMA+día de semana 30d MEDIUM, Tier 3 (90d+) patrón semanal+tendencia 60d HIGH. Caché Redis TTL 6h; `?refresh=true` fuerza recálculo. `ForecastPanel` en `DashboardAnalysisScreen` muestra LineChart de 3 series.
- Tabla `analytics_events` — log insert-only de métricas anonimizadas por vertical (sin `tenant_id`, sin PII). `HealthScoreService` emite un evento en cada recálculo. `AnalyticsRepository.compute_margin_benchmark()` usa `percentile_cont(p10/p25/p50/p75)` de PostgreSQL con mínimo 5 muestras (90 días) para derivar benchmarks data-driven. Cuando hay suficientes datos, `HealthScoreService` los pasa al health engine reemplazando el benchmark estático del JSON. `GET /admin/analytics/benchmarks` (SUPERADMIN) expone benchmarks vigentes con `source=data_driven|static`.
- `decision_audit_log` tiene 3 columnas nuevas (`tokens_input`, `tokens_output`, `tokens_total`, todos `INTEGER DEFAULT 0`) y 3 campos nuevos en `decision_data` para chats con agentes: `ceo_target_agent` (a qué agente despachó el CEO), `sub_agent_name` (agente que respondió) y `token_calls` (array de objetos `{source, model, input_tokens, output_tokens}` por cada llamada LLM del turno). Migración: `20260429_0001`.

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
# Levantar stack completo (API + Celery + PostgreSQL + Redis)
make dev                    # Docker Compose con hot reload
make dev-bg                 # En background
make stop                   # Detener
make logs                   # Tail logs
make shell                  # bash en el container

# Ejecutar tests
make test                   # pytest con cobertura (mínimo 50%)
make test-fast              # pytest sin cobertura
make test-watch             # modo watch con watchfiles
make test-file FILE=app/tests/api/v1/test_auth.py  # archivo específico
pytest app/tests/api/v1/test_auth.py::test_login -v  # test específico

# Linting, formato y tipos
make lint                   # ruff check app/
make format                 # ruff format app/ --fix
make typecheck              # mypy app/ (strict=true)
make check                  # lint + typecheck

# Migraciones (directorio correcto: backend/app/persistence/migrations/versions/, NO backend/alembic/versions/)
make migrate                # alembic upgrade head
make migrate-down           # alembic downgrade -1
make migrate-create MSG="descripcion"  # nueva migración con auto-detección
make migrate-history        # historial de migraciones

# Demo data
make seed-demo              # carga 3 tenants demo con datos calibrados
make reset-demo             # resetea y reseeds tenants demo
make db-reset               # ⚠️ PELIGROSO: borra y recrea la DB
```

### Frontend (correr desde `frontend/`)

```bash
npm run dev          # dev server en :3000
npm run build        # build de producción
npm run lint         # ESLint (next lint)
npm run type-check   # tsc --noEmit
npm run test         # Jest (unit tests)
```

### Variables de entorno

- Backend: copiar `backend/.env.example` → `backend/.env`
- Frontend: copiar `frontend/.env.local.example` → `frontend/.env.local` (`NEXT_PUBLIC_API_URL`)
- En producción las variables se inyectan desde Railway/Vercel, nunca se commitean

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

**Regla crítica:** Los datos crudos de transacciones NUNCA llegan al Health Engine directamente. Todo pasa por `BusinessStateLayer.compute()` primero, que normaliza el estado financiero en un `BusinessState` con 5 scores por dimensión (0–100): `liquidity`, `profitability`, `cost_control`, `sales_momentum`, `debt_coverage`.

### Capas y responsabilidades

| Capa | Path | Responsabilidad |
|------|------|-----------------|
| API | `app/api/v1/` | Routing, validación Pydantic, auth deps |
| Deps | `app/api/v1/deps.py` | JWT decode, `get_current_user`, `get_current_tenant`, `require_role()` |
| Application | `app/application/services/` | Orquestación: `auth_service`, `cash_service`, `conversation_service`, `google_oauth_service`, `health_score_service`, `onboarding_service`, `pending_action_service`, `score_trigger_service`, `stock_service`, `supplier_service`, `business_memory_service`, `agent_memory_service`, `forecast_service`, `analytics_service` |
| Commands | `app/application/commands/` | Writes CQRS (ej. `create_tenant.py`) |
| Queries | `app/application/queries/` | Reads CQRS (ej. `get_health_score.py`) |
| DTOs | `app/application/dto/` | Objetos de transferencia entre capas (ej. `auth_dto.py`) |
| DB middleware | `app/application/db/tenant_context.py` | Inyecta tenant_id en el contexto de SQLAlchemy |
| **Agents** | `app/application/agents/` | Capa multiagente LLM (ver sección Agentes) |
| Domain | `app/domain/` | Entidades puras Python: `HealthScore`, `BusinessProfile`, etc. |
| BSL | `app/state/business_state_layer.py` | Agrega revenue/expenses 30 días → 5 dimension scores |
| Heuristics | `app/heuristics/` | Reglas específicas por vertical (kiosco/decoracion/limpieza) |
| Persistence | `app/persistence/` | SQLAlchemy async, repositories, modelos, Alembic |
| Jobs | `app/jobs/` | Celery workers: scores, notifications, reports, ingestion (OCR, xlsx) |
| Security | `app/application/security/` | `prompt_defense.py` (`wrap_user_input()`),  |

### API Routers (`app/api/v1/`)

Todos registrados en `router.py`. Dominios principales: `auth`, `oauth` (social login), `tenants`, `users`, `business_profiles`, `sales`, `expenses`, `products`, `health_scores`, `insights`, `momentum`, `notifications`, `files`, `ingestion`, `onboarding`, `agent` (LLM chat + conversaciones + streaming), `integrations` (estado y lifecycle de conexiones MCP Google), `forecast`, `admin`.

### Autenticación y multi-tenancy

- JWT (HS256, python-jose). Payload: `sub` (user_id), `tenant_id`, `role_code`.
- OAuth social login via `oauth.py` — identity tables: `user_auth_identity`, `user_auth_identity`.
- `get_current_tenant_id` es la dependencia que se inyecta en TODOS los endpoints de negocio.
- El `tenant_id` del JWT se usa en cada query — nunca se acepta del body/path del request.
- Roles: `OWNER`, `ADMIN`, `VIEWER`. Se aplica con `require_role("OWNER", "ADMIN")`.
- En producción: `/docs`, `/redoc` y `/openapi.json` están deshabilitados.

### Celery

Queues: `default`, `scores`, `notifications`, `reports`, `ingestion`.

Después de cualquier write de ventas/gastos/productos, se dispara:
```python
trigger_score_recalculation.delay(str(tenant_id), triggered_by="...")
```

Beat schedule: momentum update + weekly email (lunes 08:00 ART).

### Scores: dos sistemas distintos

**`ScoreLevel` (dominio — `app/domain/health_score.py`)** — clasifica el `total_score` del `HealthScore`:

| Rango | ScoreLevel |
|-------|-----------|
| 90–100 | `EXCELLENT` |
| 75–89 | `GOOD` |
| 60–74 | `FAIR` |
| 40–59 | `WARNING` |
| 0–39 | `CRITICAL` |

`HealthScore.needs_attention` → `True` si `level in (CRITICAL, WARNING)`.

> Los dos sistemas (`ScoreLevel` y `severity_from_score`) son **intencionalmente distintos** y no deben unificarse: uno clasifica el estado del negocio, el otro decide la severidad de una notificación. Tienen cutoffs distintos por diseño.

**`severity_from_score()` (insights — `app/heuristics/insight_templates.py`)** — severidad de notificación del score total entero:

| Rango | Severidad |
|-------|-----------|
| ≥80 | `LOW` |
| ≥60 | `MEDIUM` |
| ≥30 | `HIGH` |
| <30 | `CRITICAL` |

### Heuristics e Insights

- Los insights son **template-based**, no generados por LLM. Templates en `app/heuristics/insight_templates.py`.
- Risk codes disponibles: `CASH_LOW`, `MARGIN_LOW`, `STOCK_CRITICAL`, `SUPPLIER_DEPENDENCY`.
- Benchmarks de margen por vertical — fuente canónica: JSONs en `app/application/data/heuristics/{kiosco_almacen,limpieza,decoracion_hogar}.json`, sección `margin` (campos: `critical_below`, `warning_below`, `healthy_min`, `healthy_max`). El loader `app/heuristics/verticals/loader.py → load_margin_benchmark()` los lee; si el JSON no existe o está malformado cae a hardcoded. Cuando `AnalyticsRepository` tiene ≥5 muestras de los últimos 90 días, `HealthScoreService` usa el benchmark data-driven en lugar del estático.
- Para agregar un nuevo tipo: añadir entrada en `TEMPLATES`, agregar rama en `render_insight()`, y emitirlo desde el Health Engine.

### Capa de Agentes LLM (`app/application/agents/`)

8 agentes especializados coordinados por AgentCEO. El cliente NUNCA elige el agente destino.

| Agente | Context Budget | Responsabilidad |
|--------|---------------|-----------------|
| AgentCEO | 2.000 tokens | Router/coordinador, nunca accede a datos de negocio directamente |
| AgentCash | 3.000 tokens | Caja, ventas, cobros, pagos e import básico desde Google Sheets |
| AgentStock | 3.000 tokens | Inventario, quiebres, rotación, merma |
| AgentSupplier | 3.500 tokens | Proveedores, Gmail vía MCP o modo informacional |
| AgentHealth | 4.000 tokens | Score de salud, narrativa ejecutiva |
| AgentHelper | 2.500 tokens | FAQ, manual, guía funcional |
| AgentCalendar | 3.000 tokens | Eventos Google Calendar vía MCP |
| AgentSync | 4.000 tokens | Operaciones Google Sheets / Docs vía MCP |

> El estado real de cada agente cambia rápido — verificar `backend/app/application/agents/<agent>/agent.py` antes de asumir que está stub o implementado. La tabla ya no trackea fases.

**Modelos LLM:**
- AgentCEO: `claude-sonnet-4-5`
- ChatOrchestrator: `claude-haiku-4-5-20251001` para la respuesta conversacional final
- Cash, Stock, Health, Helper: `claude-haiku-4-5`
- Verificar cada agente antes de asumir el modelo exacto; no todos usan el mismo sufijo/versionado.

**Cliente Anthropic** (`app/integrations/anthropic_client.py`) — todos los agentes deben obtener el cliente via `get_anthropic_async_client()`. Centraliza el manejo de `ANTHROPIC_API_KEY` y permite inyección de mocks en tests sin key real. No instanciar `anthropic.AsyncAnthropic` directamente en agentes.

**Dependencia:** `anthropic` SDK — debe estar en `requirements.txt` con versión pinneada.

**Contratos fijos** (`app/application/agents/shared/schemas.py`):
- `AgentRequest`: `{ request_id, user_id, business_id, message, attachments, conversation_id }` — sin `agent_target`
- `AgentResponse`: `{ request_id, agent_name, status, risk_level, requires_approval, confidence, result, pending_action_id?, question?, message?, usage? }`
- `status`: `"success" | "requires_approval" | "requires_clarification" | "requires_google_auth" | "error"`
- `confidence`: `"HIGH" | "MEDIUM" | "LOW"` — nunca un float
- `LLMCall`: `{ source: str, model: str, input_tokens: int, output_tokens: int }` — captura de una llamada LLM individual
- `UsageSummary`: `{ calls: list[LLMCall] }` con propiedades calculadas `total_input`, `total_output`, `total`. Presente en `AgentResponse.usage`; `None` si el turno no hizo ninguna llamada LLM.

**ActionType** (`shared/schemas.py`) — catálogo cerrado de 16 valores:

```
REGISTER_SALE          REGISTER_CASH_INFLOW    REGISTER_EXPENSE
REGISTER_PURCHASE      REGISTER_CASH_OUTFLOW   UPDATE_STOCK
REGISTER_STOCK_LOSS    CREATE_PURCHASE_SUGGESTION
IMPORT_TABULAR_FILE    PARSE_DOCUMENT_FILE     GENERATE_HEALTH_REPORT
ANSWER_HELP_REQUEST    CREATE_SUPPLIER_DRAFT   CLASSIFY_GMAIL_MESSAGE
SYNC_TO_GOOGLE         CREATE_CALENDAR_EVENT
```

Nada fuera de esta lista puede ejecutarse. Agregar o quitar una acción requiere actualizar también `RiskEngine` y sus tests.

**Auto-ejecución:** `REGISTER_EXPENSE` se ejecuta automáticamente sin pedir confirmación al usuario (`_AUTO_EXECUTE_ACTION_TYPES` en `api/v1/agent.py`). El mensaje de respuesta se prefija con "Gasto registrado.".

**Deduplicación por fingerprint** (`operation_fingerprints`): antes de ejecutar `REGISTER_SALE`, `REGISTER_EXPENSE` o `REGISTER_PURCHASE`, se calcula SHA-256 de `{tenant_id}:{action_type}:{amount}:{date}` y se compara contra la tabla `operation_fingerprints`. Si ya existe, la acción devuelve `execution_status: "DUPLICATE"` sin volver a insertar.

**Rate limit de chat:** 50 mensajes/día por tenant. Redis key: `rate:chat:{tenant_id}:{date}`. Se configura expiry a medianoche del día. Compartido entre `/chat` y `/chat/stream`.

**RiskEngine** (`shared/risk_engine.py`) — función determinística pura, sin LLM. `HIGH` requiere aprobación; `MEDIUM` también; `LOW` no.

**Endpoint de retry** (`POST /agent/retry/{pending_id}`): re-ejecuta acciones externas fallidas. Condiciones: `status=APPROVED`, `execution_status in (FAILED, REQUIRES_RECONNECT)`, solo `is_external=True`, y máximo 1 reintento total rastreado via `decision_audit_log` con `decision_type="AGENT_ACTION_RETRIED"`.

**`execution_status` en acciones externas:** puede ser `IN_PROGRESS` → `SUCCEEDED` | `FAILED` | `REQUIRES_RECONNECT`. El valor `REQUIRES_RECONNECT` ocurre cuando `McpToolAuthError` es lanzado; el frontend muestra un prompt para reconectar Google.

**ContextBuilder** (`shared/context_builder.py`) — helper disponible y cubierto por tests para budgets de contexto. El `ChatOrchestrator` actual arma el prompt manualmente y no lo invoca directamente, pero el orden de prioridad esperado sigue siendo:
1. `historical_data` (400 tokens)
2. `conversation_history` (1.000 tokens)
3. `recent_events` (800 tokens)
4. `current_snapshot` (600 tokens) — SIEMPRE incluido hasta aquí
5. `uploaded_files` (400 tokens) — archivos procesados del tenant
6. `agent_memory` (300 tokens) — patrones de comportamiento aprendidos
7. `business_heuristics` (300 tokens) — SIEMPRE incluido
8. `intent_and_entities` (200 tokens) — SIEMPRE incluido

**HeuristicEngine** (`shared/heuristic_engine.py`) — implementado. Carga JSON de defaults por rubro desde `app/application/data/heuristics/`. `get(business_type)` es síncrono (solo defaults); `get_async(business_type, business_id, db)` aplica `BusinessHeuristicOverride` de la DB para customización por tenant. `HeuristicConfig.to_prompt_fragment()` genera el fragmento listo para inyectar en system prompts como valores numéricos — nunca texto narrativo. Rubro desconocido hace fallback a `kiosco_almacen`.

**AgentCEO — flujo interno:** `classify_intent()` llama al LLM (max_tokens=300) para mapear el mensaje del usuario a uno de los 15 intents del `INTENT_CATALOG` (incluye `schedule_event`, `check_calendar`, `sync_google_data` para los agentes Google). El intent no reconocido cae a `ask_platform_help`. Luego `INTENT_TO_ACTION_TYPE` y `INTENT_TO_AGENT` (ambos en `ceo/agent.py`) resuelven determinísticamente el `ActionType` y el agente destino sin más LLM. Los sub-agentes se resuelven por nombre en `app/application/agents/registry.py → get_sub_agent(name)`, llamado desde `ChatOrchestrator`.

**Streaming SSE** (`POST /agent/chat/stream`): emite tres tipos de eventos:
- `{"type": "thinking", "text": "..."}` — inmediatamente al recibir el request
- `{"type": "response", "data": {...}}` — `AgentResponse` serializada
- `{"type": "error", "message": "...", "code": N}` — si hay excepción (code 429 = rate limit)
Finaliza con `data: [DONE]`. El frontend usa `sendStream()` en `useChat`, que mantiene un mensaje placeholder con `thinkingId` que se actualiza in-place.

**ChatOrchestrator** (`app/application/services/chat_orchestrator.py`) — punto de entrada real de `/agent/chat` y `/agent/chat/stream`. Carga 4 capas de contexto (fail-silencioso cada una):
1. Contexto del negocio + heurísticas (`_load_business_context`)
2. BusinessMemory: resumen financiero acumulado (`BusinessMemoryService.get`)
3. AgentMemory: patrones de comportamiento aprendidos (`AgentMemoryService.get_context_fragment`)
4. Archivos procesados: últimos 5 `UploadedFile` con `parsed_summary_json` (`_load_file_context`)

Luego: CEO clasifica intent → `registry.get_sub_agent()` despacha → si `requires_google_auth` retorna inmediato sin LLM → si `requires_approval` usa summary estructurado → si no, el orquestador genera respuesta rica con Claude → `ConversationService` guarda el turno (best-effort).

El orquestador acumula todas las `LLMCall` del turno (CEO + sub-agente + respuesta final) en `all_llm_calls` y las adjunta al `AgentResponse.usage` antes de retornar. Inyecta siempre `intent` y `target_agent` del CEO en `agent_response.result`, incluyendo los paths de corte temprano (`out_of_scope`, `requires_google_auth`).

**ConversationService** (`app/application/services/conversation_service.py`) — historial de chat: Redis como caché caliente (TTL 24h) con fallback a PostgreSQL (`agent_conversation_context`). Ventana deslizante de los últimos 10 turnos; los más viejos se descartan. El `conversation_id` es UUID generado en el cliente.

**EventBus** (`shared/event_bus.py`) — wrapper fino sobre Celery (`send_task`) para emitir eventos internos desacoplados. Hoy no existe una capa de suscripción del CEO dentro del código; la coordinación downstream ocurre a través de tasks/event handlers.

**Extras por agente:**
- `agents/health/scorer.py` — lógica de scoring especializada usada por AgentHealth
- `agents/supplier/preflight.py` — validaciones previas al envío de borradores a Gmail

### Sistema de Memoria (tres capas)

| Capa | Servicio | Backend | TTL cache | Qué almacena |
|------|----------|---------|-----------|--------------|
| Conversacional | `ConversationService` | Redis + PostgreSQL | 24h | Últimos 10 turnos del chat |
| Negocio | `BusinessMemoryService` | Redis + PostgreSQL | 5min | Resumen financiero acumulado (ventas, gastos, alertas) |
| Agente | `AgentMemoryService` | Redis + PostgreSQL | 5min | Patrones de comportamiento: método de pago, monto promedio de venta (Welford), categorías de gasto, top ActionTypes |

`AgentMemoryService.record_action(tenant_id, action_type, payload)` se llama en `pending_action_service.py` después de cada acción confirmada (fail-silent). La confianza crece con ocurrencias: `confidence = min(1.0, 0.5 + occurrence_count/20)`. Los patrones se inyectan en el system prompt del orquestador como texto natural.

**Tabla PostgreSQL:** `agent_memory` — columnas: `id`, `tenant_id`, `key`, `value` (JSONB), `occurrence_count`, `confidence`, `last_seen_at`, `created_at`. Unique constraint `(tenant_id, key)`.

### Integración MCP Google

Feature flag: `ENABLE_GOOGLE_MCP_TOOLS=false` (default). Solo activa llamadas reales al MCP server cuando es `true` y `MCP_SERVER_URL` está configurado.

**OAuth MCP separado del login social:** el MCP server debe usar variables propias `GOOGLE_MCP_OAUTH_CLIENT_ID`, `GOOGLE_MCP_OAUTH_CLIENT_SECRET` y `GOOGLE_MCP_OAUTH_REDIRECT_URI`. El redirect debe apuntar al MCP server (`https://<mcp-host>/auth/callback`), no al callback del backend de login social (`/api/v1/auth/...`). Google no requiere comprar tokens: emite `access_token`/`refresh_token` gratis al completar el consent flow y el token exchange.

**Arquitectura port/adapter:**
- `app/application/ports/mcp_gateway.py` — interface abstracta `McpToolGateway`
- `app/integrations/mcp/http_gateway.py` — implementación HTTP JSON-RPC 2.0 (`HttpMcpGateway`)
- `app/integrations/mcp/google_mcp_service.py` — `GoogleMcpService` con allowlist por agente
- `app/integrations/mcp/exceptions.py` — `McpToolAuthError` para errores de autenticación Google vía MCP

**Allowlists por agente:** cada agente solo puede llamar las herramientas MCP de su frozenset (`google.gmail.*`, `google.calendar.*`, `google.sheets.*`, `google.docs.*`).

**Flujo `requires_google_auth`:** Si un agente detecta que falta acceso Google, devuelve `status="requires_google_auth"`. El orquestador lo propaga sin LLM. El frontend hoy muestra el estado en el chat y deriva a `/apps`.

**Gateway condicional en registry:** `AgentCalendar` y `AgentSync` reciben `gateway=HttpMcpGateway(...)` solo si `ENABLE_GOOGLE_MCP_TOOLS=true` y `MCP_SERVER_URL` está definido; de lo contrario `gateway=None` y operan en modo informacional.

**Router `integrations`** (`app/api/v1/integrations.py`) — expone el lifecycle de conexiones MCP Google:
- `GET /integrations/google/status` — estado de la conexión (`CONNECTING` / `CONNECTED` / `DISCONNECTED` / etc.) del tenant actual
- `POST /integrations/google/connect/start` — inicia el flujo OAuth hacia el MCP server; crea registro `GoogleMcpConnection(status=CONNECTING)` si no existe
- `POST /integrations/google/disconnect` — revoca tokens y pone `status=DISCONNECTED`

**Modelo ORM:** `GoogleMcpConnection` (`app/persistence/models/google_mcp_connection.py`) — tabla `google_mcp_connections`, columnas clave: `tenant_id`, `user_id`, `status`, `scopes_granted` (JSONB), `connected_at`.

**`google_oauth_tokens`** — tabla añadida en `20260424_0002` para que el MCP server persista tokens OAuth de Google (access + refresh cifrados). No tiene modelo ORM en el backend; el MCP server la gestiona directamente. Si el callback falla, `mcp_server/app/auth/service.py` guarda `last_error_code` (`token_exchange_failed:*`, `missing_access_token`, etc.) y el backend promueve la conexión a `ERROR` en el siguiente status check. Además, un `GoogleMcpConnection(status=CONNECTING)` pendiente por más de 10 minutos expira como `oauth_callback_timeout`, evitando quedar indefinidamente en `CONNECTING`.

**Nota de compatibilidad:** Pueden existir rastros históricos de integraciones Google anteriores, pero la integración viva hoy es la del MCP server.

**Prompt defense:** Todo input de usuario que llegue a un LLM debe pasar por `wrap_user_input()` de `app/application/security/prompt_defense.py` antes de incluirse en un prompt. El mismo módulo expone `is_valid_action_type(action_type)` para validar que el output de un LLM sea un ActionType del catálogo cerrado — usar al parsear cualquier respuesta LLM que deba devolver un action_type.

### Archivos e ingestión

**Servicio compartido:** `app/application/services/file_parsing.py` centraliza:
- sanitización de nombres
- detección segura de MIME
- generación canónica de `parsed_summary_json`
- helpers para contexto de chat (`summary_columns`, `summary_row_count`, `preview_value_from_summary`)

**Uploads para chat** (`api/v1/files.py`):
1. `POST /files/upload?purpose=chat`
2. Valida tamaño y tipo soportado
3. Parsea el contenido **sincrónicamente**
4. Guarda `UploadedFile` con `processing_status=DONE` y `parsed_summary_json`

Si el parseo falla para chat, el endpoint devuelve `422` y no persiste un archivo roto.

**Ingestión de datos** (`api/v1/ingestion.py` + `jobs/ingestion_worker.py`):
1. **Upload** — `POST /ingestion/upload?file_hint=ventas|gastos|stock|general`
2. **Parse async** — Celery task por tipo (`process_spreadsheet`, `process_text_document`, `process_image_ocr`)
3. **Preview** — `GET /ingestion/files/{file_id}/preview`
4. **Confirm** — `POST /ingestion/files/{file_id}/confirm`

La confirmación humana sigue siendo obligatoria antes de insertar ventas/gastos/productos.

### Observabilidad

- Logging con `structlog`. Usar `from app.observability.logger import get_logger` → `get_logger(__name__)` en todos los módulos.
- `bind_request_context(tenant_id=..., user_id=...)` se llama en `deps.py` para cada request.
- Rate limiting con `slowapi` (200 req/min por defecto).

---

## Arquitectura del frontend

- Next.js 15 App Router. Rutas protegidas bajo `src/app/(protected)/`, públicas bajo `src/app/(public)/`.
- Estado global: Zustand (`src/stores/`). Server-state: TanStack Query (`src/lib/queryClient.ts`).
- HTTP client: axios wrapper en `src/lib/api.ts` con `NEXT_PUBLIC_API_URL`.
- UI: Tailwind CSS + componentes en `src/components/ui/`. Sin librería de componentes externa.
- Validación de forms: Zod (`src/validation/`).
- Charts: Recharts.

### Estado visual actual

- Tema base dark centralizado en `src/styles/globals.css` y `tailwind.config.ts` con tokens `vektor-*`.
- Tipografías globales cargadas en `src/app/layout.tsx`: `Barlow Condensed` para UI y `Inter` para cuerpo de texto.
- Componente reusable `Tooltip` en `src/components/ui/Tooltip.tsx`: hover-only, delay 300ms, dark surface, arrow y animación `fade-slide-up`.
- `EconomicTicker` en `src/components/dashboard/EconomicTicker.tsx` se renderiza desde `src/app/(protected)/layout.tsx` solo en rutas de dashboard; en mobile se reemplaza por botón/modal.
- La navegación del dashboard usa `DashboardLaunchpadNav` para tabs/dots compartidos entre `/dashboard` y `/dashboard/analisis`.
- Los datos agregados/mocks del rediseño del dashboard viven en `src/features/dashboard/dashboardData.ts`; cuando falte backend real, dejar `// TODO:` explícito con el endpoint esperado.

### Organización del frontend

| Directorio | Responsabilidad |
|------------|-----------------|
| `src/features/` | Módulos por feature: `auth`, `chat`, `dashboard`, `onboarding`, `ingestion`, `notifications` |
| `src/services/` | Capa de llamadas HTTP por dominio: `auth`, `sales`, `expenses`, `products`, `health_score`, `dashboard`, `momentum`, `notifications`, `ingestion`, `onboarding`, `files`, `agent` |
| `src/stores/` | Zustand: `authStore` (JWT + user), `toastStore` |
| `src/hooks/` | Custom hooks: `useAuth` |
| `src/types/api.ts` | Tipos TypeScript de respuestas de la API |
| `src/components/auth/AuthHydrationBoundary.tsx` | Hidrata auth desde localStorage antes de renderizar rutas protegidas |

### Rutas protegidas (`src/app/(protected)/`)

| Ruta | Componente | Descripción |
|------|-----------|-------------|
| `/chat` | `features/chat/ChatPage.tsx` | **Home principal** — chat de página completa, sin panel flotante |
| `/dashboard` | `features/dashboard/` | Pantalla 1 del launchpad: health score hero + KPIs de caja, margen, stock y proveedores |
| `/dashboard/analisis` | `features/dashboard/` | Pantalla 2 del launchpad: charts + ForecastPanel (3 series: ingresos/egresos/neto) + panel gastos por categoría + panel stock crítico |
| `/sales` | `(protected)/sales/page.tsx` | Analytics + lista de ventas con KPIs y filtros |
| `/expenses` | `(protected)/expenses/page.tsx` | Analytics + lista de gastos con KPIs y filtros |
| `/products` | `(protected)/products/page.tsx` | Catálogo con KPIs de stock e inventario; acepta filtro por query param `?stock=ok|low|out` |
| `/apps` | `(protected)/apps/page.tsx` | Integraciones Google con conexión directa, disconnect y estado de permisos |
| `/settings` | `(protected)/settings/page.tsx` | Cuenta y configuración |

### Rutas públicas (`src/app/(public)/`)

| Ruta | Descripción |
|------|-------------|
| `/oauth/callback?session_id=` | Callback de Google OAuth login — llama `POST /auth/oauth/google/exchange` |

### Chat (Sprint 5)

- `/chat` es la home post-login. Todos los redirects post-auth apuntan a `/chat`, no `/dashboard`.
- `ChatPanel.tsx` se mantiene en el repo pero **no** está registrado en el layout global.
- `conversation_id`: proviene de `useChatStore` (Zustand). `newConversation()` genera un nuevo UUID. No se espera del servidor.
- Adjuntos: `AttachmentPicker.tsx` — hasta 3 archivos (PDF/XLSX/CSV/TXT/DOCX/PPTX/PNG/JPG), se suben inmediatamente a `POST /files/upload?purpose=chat` antes de enviar el mensaje. Los `file_id` se pasan en el body del agente.
- Layout condicional en `(protected)/layout.tsx`: chat usa `flex flex-col overflow-hidden`, otras páginas usan el wrapper con padding y scroll normal.
- `useChat` expone `send` (REST) y `sendStream` (SSE). Ambos leen/escriben en `useChatStore`. `sendStream` muestra un placeholder "pensando" y lo actualiza in-place con la respuesta.
- `MUTATING_ACTIONS` en `useChat.ts`: 7 action types que triggerean `queryClient.invalidateQueries` en `sales-entries`, `expenses-entries`, `products`, `inventory` (síncronos) y `health-scores` (fire-and-forget).

### Google OAuth (Sprint 5)

Flujo login federado:
1. `LoginForm` → `POST /auth/oauth/google/start` → `window.location.href = authorization_url`
2. Google → `/oauth/callback?session_id=...`
3. `POST /auth/oauth/google/exchange` → `AuthResponse` (nuevo usuario) **o** `OAuthLinkRequiredResponse` (email ya existente)
4. Si `link_required`: formulario de contraseña para vincular → `POST /auth/oauth/google/link-pending`

### Integraciones externas vía MCP

Estado actual:
1. Las integraciones Google activas dependen de `ENABLE_GOOGLE_MCP_TOOLS=true`
2. El backend llama al MCP server vía `HttpMcpGateway`
3. El frontend expone `/apps` como pantalla operativa de integraciones: iniciar conexión, reconectar y desconectar Google
4. Si una acción externa falla con `REQUIRES_RECONNECT`, el frontend guía al usuario de vuelta a `/apps`
5. En producción, `GOOGLE_MCP_OAUTH_REDIRECT_URI` debe estar registrado exactamente igual en Google Cloud Console como Authorized redirect URI.
6. En `/apps`, el botón de conexión abre una ventana en blanco durante el click y luego la redirige a Google cuando llega `auth_url`; esto evita bloqueo de popups en navegadores.

### Endpoints frontend agregados recientemente

- `GET /api/economia` — agrega dólar oficial/blue/MEP/CCL + inflación/REM/tasa BCRA con `revalidate = 1800`
- `GET /forecast/cash` — forecast de caja desde `dashboard.service.ts → fetchCashForecast()`; tipos `CashForecastResponse` / `ForecastPoint` en `api.ts`
- `GET /insights/breakdown?days=N` — desglose de gastos por categoría y top proveedores; tipos `BusinessBreakdownResponse`, `CategoryBreakdownItem`, `SupplierBreakdownItem`, `ProductStockItem` en `api.ts`
- `GET /health-scores/history/v2` — historial de scores usado en `DashboardAnalysisScreen` para las series de margen y stock del gráfico de líneas; consume `HealthScoreV2Response[]` desde `dashboard.service.ts → fetchHealthScoreHistory()`
- `GET /insights/current` — insight real del tenant (template-based) consumido por `InsightBlock` en el dashboard de análisis. El stub `/api/analisis/insight` ya no se usa.

---

## Historial de sprints

| Sprint | Estado | Descripción |
|--------|--------|-------------|
| 1 | ✅ Completo | Auth social (Google OAuth), modelo `user_auth_identity` |
| 2 | ✅ Completo | Google OAuth login frontend, callback `/oauth/callback` |
| 3 | ✅ Completo | Primeras integraciones Google / Gmail |
| 4 | ✅ Completo | Pending Actions externas — lifecycle (`/pending-actions/{id}/execute`), retry con guard `is_external`, idempotency_key, integración `EXTERNAL_SYSTEMS` |
| 5 | ✅ Completo | Chat como página central (`/chat` = home), Google OAuth login federated, adjuntos en chat, analytics Ventas/Gastos/Productos |
| 6 | ✅ Completo | Integración MCP Google: AgentCalendar + AgentSync, BusinessMemoryService, AgentMemoryService, file context en chat |
| 7 | ✅ Completo | Data moat: heurísticas desde JSON, alertas accionables, SmartTable+CSV, BSL breakdown, forecast 3-tiers, analytics_events data-driven |
| 8 | ✅ Completo | Auditoría completa de agentes + token tracking: `LLMCall`/`UsageSummary` en schemas, captura de `response.usage` en los 7 agentes + CEO + orquestador, `decision_audit_log` con columnas `tokens_*` y campos `ceo_target_agent`/`sub_agent_name`/`token_calls`. Dashboard analisis cablea `/health-scores/history/v2` e `/insights/current` (reemplaza mocks). |

### Migraciones de compatibilidad recientes

- `20260401_0003_restore_chat_context_and_heuristics.py`
  - restaura `business_heuristic_overrides`
  - restaura `agent_conversation_context`
  - agrega `business_profiles.heuristics_version`
- `20260406_0001_stub.py` y `20260406_0002_stub.py`
  - no-op stubs para conservar continuidad Alembic después de retirar migraciones viejas de Google
- `20260421_0001_add_memory_tables.py` y `20260421_0002_add_agent_memory.py`
  - continúan la cadena actual de memoria
- `20260424_0001_add_google_mcp_connections.py`
  - agrega tabla `google_mcp_connections` (ORM: `GoogleMcpConnection`)
- `20260424_0002_add_google_oauth_tokens.py`
  - agrega tabla `google_oauth_tokens` para persistencia de tokens OAuth del MCP server (sin modelo ORM por ahora)
- `20260427_0001_add_analytics_events.py`
  - agrega tabla `analytics_events` (insert-only, sin tenant_id): `vertical_code`, `margin_ratio`, `cash_ratio`, `total_score`, `liquidity_score`, `profitability_score`, `cost_control_score`, `sales_momentum_score`, `debt_coverage_score`, `event_date`
  - modelo ORM: `AnalyticsEvent` en `app/persistence/models/analytics_event.py`
- `20260429_0001_add_token_tracking_to_audit_log.py`
  - agrega columnas `tokens_input`, `tokens_output`, `tokens_total` (INTEGER, DEFAULT 0) a `decision_audit_log`
  - crea índice compuesto `ix_decision_audit_log_tenant_created` sobre `(tenant_id, created_at)` para queries de analytics por período

Post-Sprint 5/6/7/8: hardening de infra en Railway (Alembic chain, manifests de worker/beat, `/ready` endpoint para readiness probes). Migraciones manuales contra Neon con psycopg2 directo cuando la cadena Alembic está rota.

Post-Sprint 8 (2026-04-30): email reemplazado de SMTP→Resend HTTP API. Railway bloquea el puerto 587 saliente; `app/integrations/smtp.py` ahora usa `httpx` contra `https://api.resend.com/emails`. Variables requeridas en Railway: `RESEND_API_KEY` (key de Resend, empieza con `re_`) y `SMTP_FROM_EMAIL=noreply@vektor.app`. Dominio `vektor.app` verificado en Resend (DKIM + SPF en Cloudflare DNS). `SMTP_PASSWORD` se mantiene como alias legacy/fallback en settings pero ya no es la variable principal.

---

## Reglas de trabajo

- **Mostrar plan antes de escribir código y esperar confirmación.**
- Tipos estrictos siempre (`mypy strict=true`). Cada endpoint necesita schema Pydantic de request y response.
- `tenant_id` enforced en CADA query de negocio, obtenido del JWT, nunca del cliente.
- Los scores se recalculan solo ante cambios de datos (Celery async), no en cada request.
- Todo decision generada se registra en `decision_audit_log` (insert-only, nunca update/delete).
- Fail-closed en cualquier write sensible: ante error, no continuar.
- En la capa de agentes: el catálogo de `ActionType` es cerrado — no agregar acciones fuera de los 16 definidos sin actualizar el `RiskEngine` y los tests.
- System prompts de agentes: inyectar heurísticas como valores numéricos, nunca como texto narrativo ("margen del 12% al 18%", no "el margen es bueno si está en rango saludable").
- Todo input de usuario a LLM debe pasar por `wrap_user_input()` antes de incluirse en un prompt.

---

## Tests

- Framework: pytest + pytest-asyncio (`asyncio_mode = "auto"`).
- DB de tests: SQLite + aiosqlite en memoria (`conftest.py`).
- Cobertura mínima: **50%** en local (`--cov-fail-under=50`), **60%** en CI (`ci-backend.yml`).
- Correr un test de dominio: `pytest app/tests/domain/test_health_score.py -v --no-cov`

---

## CI

- `.github/workflows/ci-backend.yml` — ruff + mypy + pytest (cov ≥ 60%) + Docker build. Triggers on `backend/**` changes to `main`/`develop`.
- `.github/workflows/ci-frontend.yml` — tsc + ESLint + `next build`. Triggers on `frontend/**` changes to `main`/`develop`.

## Deploy

### Topología de producción (Railway + Vercel) — beta

Durante la beta Véktor corre en un **único servicio Railway** (sin worker/beat separados). Los jobs de Celery están pausados — el front funciona, los scores async no.

| Servicio | Manifiesto | Start command |
|----------|-----------|---------------|
| `vektor-api` | `backend/railway.toml` | `sh scripts/start_web.sh` → uvicorn solo |
| Postgres | Neon (externo, no Railway) | — |
| Redis | Railway managed (opcional en beta) | — |
| Frontend | Vercel, root `frontend/` | `next start` |

`backend/scripts/start_web.sh` lanza uvicorn con `$PORT` y `$UVICORN_WORKERS` (default 1). **No** corre Alembic — migrations tienen que aplicarse a mano contra la DB de Neon (ver `make migrate` con `DATABASE_URL` apuntando a Neon).

**Graceful bootstrap:** `app/bootstrap.py` captura excepciones de DB/Redis y loguea `bootstrap.*.unavailable` en vez de crashear el lifespan. Esto significa que uvicorn arranca y `/health` responde incluso si Postgres o Redis están caídos — los endpoints que los necesitan van a fallar en runtime, pero Railway healthcheck queda verde.

**Health vs readiness** (definidos en `app/main.py`):
- `/health` — liveness, siempre 200 si el proceso Python responde. Sin auth. Usado por `healthcheckPath` en `railway.toml` (timeout 120s).
- `/ready` — readiness real: chequea DB + Redis, devuelve 503 si alguno falla. No usado por Railway actualmente.

**Volver a separar worker/beat** cuando haya tráfico real: restaurar `backend/worker/` y `backend/beat/` con sus `railway.toml` (siguen en el repo como referencia) y quitar el `/health` healthcheck de esos servicios.

## Demo

Acceso: `http://localhost:3000/demo` (password `Demo1234!` para todos):

| Email | Vertical | Score | Estado |
|-------|----------|-------|--------|
| demo.kiosco@vektor.app | Kiosco | 74 | Saludable |
| demo.limpieza@vektor.app | Limpieza | 51 | En riesgo |
| demo.deco@vektor.app | Decoración | 62 | Estable |

Cada tenant incluye 8 semanas de historial, momentum profile, insights, 8–15 productos y 30 días de transacciones. Regenerar con `make reset-demo`.
