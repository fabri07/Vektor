# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

---

## Start here — invariantes que evitan bugs

Leé esto antes de tocar código. El detalle completo está más abajo.

1. **Chat** entra siempre por `ChatOrchestrator` → AgentCEO clasifica → sub-agente. El cliente nunca elige el agente destino.
2. **Aritmética financiera** SOLO por `DeterministicFinance`. Los LLM nunca calculan montos.
2b. **Motor estadístico** (`shared/stats_engine.py`): calcula con numpy + numpy-financial; el LLM solo narra. scipy/statsmodels diferidos. Guardas de muestra (n < mínimo → `insufficient_data`, no-invention rule).
2c. **Métricas de negocio** (ventas, márgenes, caja, fiado, stock): SOLO por **FactsService** (`app/application/services/facts_service.py` + `facts_provider.py`, pandas determinístico). Es la única fuente de verdad — dashboard, chat y health deben leer el MISMO `BusinessFact`; nadie más agrega (ni el frontend, ni SQL inline en endpoints). Estado: fase 1 lista (servicio + provider + test de reconciliación); el cableado de consumidores está en curso — mientras tanto `DeterministicFinance` sigue vigente para la aritmética puntual del chat y NO se le agrega métrica nueva a ningún otro lado.
2d. **`inventory_movements.occurred_at`** = fecha de NEGOCIO; `created_at` = fecha de carga. Toda heurística temporal sobre el ledger usa `COALESCE(occurred_at, created_at)`; para pares de movimientos importados, el timing de inserción NUNCA es evidencia suficiente de duplicado (incidente don pedro 2026-07).
3. **`tenant_id`** sale del JWT en cada query de negocio — nunca del body/path.
4. **`ActionType`** es un set cerrado (31 valores en `shared/schemas.py`); agregar/quitar obliga a tocar `RiskEngine` + tests.
5. **No-invention:** con `confidence == LOW` (`data_completeness < 50`) → empty state y pedir datos. Nunca maquillar scores con defaults (`or 70`, `or 50`); si `0` es válido, usar `if value is None`, no `or`.
5b. **`data_completeness_score` pondera por PROCEDENCIA** (`business_state_service._compute_completeness`): un input OBSERVADO (transacciones, arqueo, maestros cargados) vale su peso completo; uno sólo DECLARADO en el onboarding vale `PESO_DECLARADO` (0.4). Los seis declarados y ninguno observado dan 40 — por debajo de la compuerta de 50 a propósito: contestar un formulario no habilita conclusiones sobre un negocio del que no se registró un movimiento. **Nunca calcular completeness sobre los `*_est`**, que ya traen el fallback a las estimaciones adentro: eso mide "¿contestó la encuesta?", no "¿tengo datos?".
6. **Auditoría + defensa:** toda decisión → `decision_audit_log` (insert-only); todo input de usuario a un LLM → `wrap_user_input()`.
7. **Mostrar plan antes de escribir código y esperar confirmación.** Responder en español (Argentina).

> Historial de sprints y cadena de migraciones: `docs/CHANGELOG-internal.md`.

---

## Proyecto

Véktor es una plataforma SaaS de salud financiera para PYMEs argentinas (kioscos, decoración hogar, limpieza). Multi-tenant, monolito modular en v1.

### Estado actual importante

- El chat productivo entra por `ChatOrchestrator`; no despacha directo desde el router al sub-agente.
- Los clientes Anthropic se construyen vía `app/integrations/anthropic_client.py`; no instanciar `anthropic.AsyncAnthropic()` directo en agentes.
- Integraciones Google son **MCP-based** (`ENABLE_GOOGLE_MCP_TOOLS` + `MCP_SERVER_URL`). `Google Login` social vía `/auth/oauth/google/*` es independiente — no confundir.
- Frontend `/apps`: conexión Google, estado de integraciones, reconnect cuando una acción devuelve `REQUIRES_RECONNECT`.
- Cadena Alembic incluye `20260401_0003_restore_chat_context_and_heuristics.py` y stubs `20260406_0001/0002_stub.py`.
- Frontend: tema dark unificado (`vektor-night`, `vektor-ink`, `vektor-surface`, `vektor-border`), tipografías `Barlow Condensed` + `Inter`.
- Landing pública (`/`): hero full-screen, social proof, highlights, y **`ScreenshotCarousel`** con 6 capturas reales en la sección "Vista previa" (el demo público autoservicio fue eliminado — ver bullet "Demo público deshabilitado").
- Dashboard dividido: `/dashboard` (health score + KPIs) y `/dashboard/analisis` (charts + insights), navegación launchpad compartida.
- Ticker económico alimentado por `GET /api/economia`. Insights de charts: `GET /insights/current` (template-based). El stub `GET /api/analisis/insight` ya no se usa.
- Benchmarks de margen desde JSON (`heuristics/verticals/loader.py → load_margin_benchmark()`). El health engine no importa los .py de vertical. Alias `kiosco` → `kiosco_almacen` normalizado en el loader.
- `HealthAlertBanner.tsx`: alertas fixed bottom-right cuando `score < 75` y `risk_code` activo (CASH_LOW, MARGIN_LOW, STOCK_CRITICAL, SUPPLIER_DEPENDENCY). Dismissable, 800ms delay.
- `SmartTable<T>` (`src/components/ui/SmartTable.tsx`): selector de columnas + exportación CSV UTF-8 BOM. Columnas con `defaultVisible` y `csvValue`.
- `GET /insights/breakdown?days=N`: gastos por categoría, top 5 proveedores, stock crítico. Alimenta dos panels en `DashboardAnalysisScreen`.
- `GET /forecast/cash`: 3 tiers según historial (Tier 1 <30d promedio 14d, Tier 2 30–90d EWMA, Tier 3 90d+ patrón semanal). Redis TTL 6h; `?refresh=true` fuerza recálculo.
- `analytics_events`: log insert-only anonimizado por vertical (sin `tenant_id`). `AnalyticsRepository.compute_margin_benchmark()` usa `percentile_cont` con ≥5 muestras/90 días para benchmarks data-driven. `GET /admin/analytics/benchmarks` (SUPERADMIN).
- `decision_audit_log`: columnas `tokens_input/output/total` (INTEGER DEFAULT 0) + campos en `decision_data`: `ceo_target_agent`, `sub_agent_name`, `token_calls`. Migración: `20260429_0001`.

### Detalle por dominio (`docs/claude/`) — leer ANTES de tocar el dominio

Este archivo tiene que quedar **bajo 40.000 caracteres** (`backend/app/tests/test_claude_md_size.py` lo exige). El detalle vive en `docs/claude/`, que no se carga solo: **antes de tocar un dominio, leé su archivo**. La doc de un cambio va en el MISMO commit y en el archivo de su dominio, no acá; acá sólo entra una línea de índice si aparece un tema nuevo o una regla que no se puede romper.

| Archivo | Qué cubre | Reglas que no se rompen |
|---------|-----------|-------------------------|
| [`ingesta.md`](docs/claude/ingesta.md) | Pipeline de archivos: E4 (convenio por columna), F8 (riesgo de columnas), F10 (3 precios, `_match_key`, `SINGLE_VALUE_FIELDS`, catálogo de campos, `stock_treatment` por hoja, traza), F-H6.d (costo facturado ≠ referencia), E6a/E6b (identidad de operación, `external_code`), E6c (huellas acotadas, límites, import asíncrono), E7a-lite, E8/E8b (documentos), F11 (borrado por procedencia), comprobante, Otros, Libro Diario, momento cero, fecha futura | Interpretación por COLUMNA, nunca por celda. Ante la duda, no hay clave de identidad. Una columna ≥ 40 % vacía no se carga sola si es informativa (elegida a mano se respeta; de identidad o monto se carga), y siempre se avisa. Una fecha futura nunca se persiste. Borrar un archivo revierte sólo lo que no tiene otra fuente, y lo dice. Ningún `U+0000` llega a la base: la entrada se limpia y el serializador lo rechaza. Las hojas se clasifican por contenido; un libro mixto se reparte por fila; lo que el archivo declara simulado no entra como real; una carga repetida masiva exige confirmación explícita. Una fórmula sin resultado se calcula sólo dentro del subconjunto de `domain/formulas.py`; lo demás queda pendiente, nunca 0. |
| [`pos.md`](docs/claude/pos.md) | POS: B0 (deuda del camino de venta), B2 (idempotencia recuperable), B3 (operación, unidades base, descuentos en centavos), B4 (scan estricto), B5 (rol `CASHIER`, permisos, precio manual), B6 (terminales), B10 (anulación), B12 (ticket/etiquetas), B13 (pantalla), B14, venta por peso, cierres de B | Fechas de negocio con `today_ar()`/`fecha_de_negocio()`, nunca `date.today()` (también en tests). El cliente no fija precios. Σ `line_total` == total. Un cajero: deny-by-default en `get_current_user`. Una línea de caja no se toca suelta. |
| [`pos-offline.md`](docs/claude/pos-offline.md) | B7·1 (precio firmado, ticket congelado), B7a (venta sin red, IndexedDB), B7b (service worker por PC), B8 (turno y arqueo), B9 (pago mixto), B11 (salida pendiente de stock), panel de diferencias | Se escribe antes de mandar; ninguna respuesta del servidor borra una venta pendiente. Un arqueo cerrado no se reescribe. Nunca unir `pos_tenders` con `pos_operation_lines`. `mixed` no lo declara ningún cliente. |
| [`suscripciones.md`](docs/claude/suscripciones.md) | Planes, `Subscription.status`, cupos con reserva, gate de escritura por efecto, consumos, servicio comercial (activar ≠ renovar) | Sin `Subscription` → 503, nunca acceso libre. Un `ActionType` nuevo nace bloqueado. Ruta de alta nueva ⇒ `test_las_rutas_con_gate_son_exactamente_estas`. |
| [`maestros.md`](docs/claude/maestros.md) | Clientes (ficha fiscal, centinela "Local"), proveedores (marca ≠ proveedor, "No identificado", remitos, marcas colapsadas), productos (estados, `UPDATE_PRODUCT`, lookup), PIN step-up, borrado protegido, `users.phone` | Los centinelas no se editan, borran ni entran al ledger. Fiado sin cliente real → 400. Gestión de roles sólo OWNER+PIN. |
| [`plataforma.md`](docs/claude/plataforma.md) | FactsService/DeterministicFinance, ValidationGate, provenance, custom fields, soft delete, DataRepair, cierre de caja diario, `transaction_date` DATETIME, resumen económico, categorías COGS/OPEX, régimen fiscal, inventario (integridad, stock negativo prohibido, reconciliación temporal), capital de socios (D·2–D·5), costo de referencia/congelado/promedio (M·1, M·2, M·P), margen bruto y neto (M·3), migraciones idempotentes + advisory lock, scripts operativos | Stock negativo prohibido. `stock_units` se revierte incremental, nunca `setattr`. Migraciones idempotentes y bajo `pg_advisory_xact_lock`. Todo engine se crea con `crear_engine()` (`engine_factory.py`), nunca con `create_async_engine` a mano. Scripts de diagnóstico: SELECT-only, nunca imprimen la URL. El margen sale sólo de `domain/margen.py` vía FactsService, y todo lector que lo muestra pasa `completitud_insuficiente`. Un retiro o aporte del socio es capital, nunca gasto. |
| [`agentes.md`](docs/claude/agentes.md) | Los 10 sub-agentes, contratos (`AgentRequest`/`AgentResponse`), catálogo de 31 `ActionType`, RiskEngine, ContextBuilder, AgentCEO (intents, rescate), handlers analíticos, `ANSWER_DATA_QUERY`, WhatsApp, memoria en 4 capas, integración MCP Google | El cliente nunca elige el agente. Los LLM nunca calculan. Todo input de usuario pasa por `wrap_user_input()`. |


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
make test                   # pytest en paralelo, SIN cobertura (loop rápido de default)
make test-cov                # misma compuerta que CI (--cov-fail-under=60)
make test-fast / make test-watch
make test-file FILE=app/tests/api/v1/test_auth.py
pytest app/tests/api/v1/test_auth.py::test_login -v

# Calidad
make lint / make typecheck / make check
make fix                    # ruff check --fix (autofix de lint, NO reformatea)
# `make format` está VEDADO (falla a propósito) — ver la regla de formato abajo

# Migraciones (dir: backend/app/persistence/migrations/versions/, NO backend/alembic/versions/)
make migrate / make migrate-down   # contra Docker Postgres local
make migrate-neon                  # contra Neon (producción) — pide DATABASE_URL del shell
make migrate-create MSG="descripcion"
make migrate-history

# Demo
make seed-demo / make reset-demo
make db-reset               # ⚠️ PELIGROSO
make seed-vertical-fields   # Upsert definiciones de campos por vertical desde JSON (idempotente)
```

### Scripts operativos

Diagnósticos, backfills y fixes puntuales en `backend/scripts/` (`_db.py` es el helper de conexión). Listado y reglas de uso: [`docs/claude/plataforma.md`](docs/claude/plataforma.md#scripts-operativos-backendscripts). Los de diagnóstico son **read-only y nunca imprimen la connection URL**; la `DATABASE_URL` la provee el usuario desde su shell.

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

**Regla crítica:** Datos crudos NUNCA llegan al Health Engine directamente. Todo pasa por `compute_business_state()` (`app/state/business_state_service.py`) → `BusinessState`, que consumen `health_score_service` y `heuristics/health_engine.py`.

### Capas y responsabilidades

| Capa | Path | Responsabilidad |
|------|------|-----------------|
| API | `app/api/v1/` | Routing, validación Pydantic, auth deps |
| Deps | `app/api/v1/deps.py` | JWT decode, `get_current_user`, `get_current_tenant`, `require_role()` |
| Application | `app/application/services/` | Orquestación: auth, cash, cash_close, conversation, google_oauth, health_score, health_config, onboarding, pending_action, score_trigger, stock, supplier, business_memory, agent_memory, forecast, analytics, deterministic_finance, validation_gate, chat_memory, field_definition, automation, data_intent_extractor, ingestion_import, data_repair, report_export, team_plan_executor, help_documentation, column_mapping, llm_column_mapper, llm_file_type_detector, pipeline_event, economic_summary, work_schedule, mcp_diagnostics |
| DB middleware | `app/application/db/tenant_context.py` | `set_tenant_context()` para las políticas RLS — **hoy no lo llama nadie**: el aislamiento real es el `tenant_id` del JWT en cada query (pendiente de decidir) |
| **Agents** | `app/application/agents/` | Capa multiagente LLM |
| Domain | `app/domain/` | Entidades puras Python |
| BSL | `app/state/business_state_service.py` | `compute_business_state()`: 30 días de transacciones (fallback a estimaciones del onboarding) → `BusinessState` |
| Heuristics | `app/heuristics/` | Reglas por vertical |
| Persistence | `app/persistence/` | SQLAlchemy async, repositories, Alembic |
| Jobs | `app/jobs/` | Celery: scores, notifications, reports, ingestion |
| Security | `app/application/security/` | `prompt_defense.py` (`wrap_user_input()`) |

### API Routers (`app/api/v1/`)

Registrados en `router.py`. Dominios: `auth`, `oauth`, `tenants`, `users`, `business_profiles`, `sales`, `expenses`, `products`, `health_scores`, `insights`, `momentum`, `notifications`, `files`, `ingestion`, `onboarding`, `agent`, `integrations`, `forecast`, `admin`, `fields`, `automations`, `settings`, `cash_closes` (`/cash-closes`), `others` (`/others`), `economic_summary` (`/economic-summary`), `suppliers` (`/suppliers`: CRUD + `GET /{id}/products` + `POST /{id}/receipts` + `POST /{id}/receipts/extract`), `customers` (`/customers`: CRUD + `POST /extract` + `POST /import/preview` + `POST /import/confirm`; centinela "Local" protegido), `owner_capital` (`/capital-movements`: alta, listado, anulación y `GET /summary` del Balance). Márgenes de pantalla: `GET /insights/margins`.

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
Post-write: `trigger_score_recalculation_after_commit(session, str(tenant_id), "...", background)` (`score_trigger_service.py`), nunca `.delay()` directo: el worker lee la base y, antes del commit, no ve el cambio.
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

### Capa de Agentes LLM, memoria e integración MCP Google

10 sub-agentes coordinados por AgentCEO + AgentChat (capa de respuesta), todos `claude-sonnet-4-6` vía `get_anthropic_async_client()`. El cliente NUNCA elige el agente destino; `ActionType` es un catálogo cerrado de 31 valores (agregar/quitar obliga a tocar `RiskEngine` + tests); `HIGH`/`MEDIUM` requieren aprobación. Memoria en 4 capas (conversacional, negocio, agente, sesión de chat). Google vía MCP detrás de `ENABLE_GOOGLE_MCP_TOOLS`. **Detalle completo** (agentes, contratos, catálogo de `ActionType`, flujo del CEO, rescate de intent, handlers analíticos, memoria, MCP): [`docs/claude/agentes.md`](docs/claude/agentes.md).

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
| `/settings` | Cuenta, configuración, panel de custom fields (`FieldDefinitionsPanel`), config de margen (`HealthConfigPanel`) y horarios laborales |
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

## Historial y migraciones

El historial de sprints (1–21), los proyectos mergeados post-Sprint-21 y la cadena completa de migraciones Alembic viven en **`docs/CHANGELOG-internal.md`** (se movieron fuera de este archivo para abaratar el costo de contexto por sesión).

**Email (referencia rápida):** Resend HTTP API vía `httpx` (`app/integrations/smtp.py`); Railway bloquea port 587. Vars: `RESEND_API_KEY` + `SMTP_FROM_EMAIL`.

---

## Reglas de trabajo

- **Mostrar plan antes de escribir código y esperar confirmación.**
- **La documentación va en el MISMO commit que el cambio, no al cerrar el bloque.** Regla desde 2026-09-11, y sale de un caso concreto: tres commits seguidos (`7de4a461`, `03ce5518`, `ca5e758c`) no tocaron este archivo, y el resultado no fue "falta documentar" sino **documentación que afirmaba lo contrario del código** —el bullet de E6c-3 seguía declarando como límite conocido una ventana de atomicidad que el arreglo 3 había cerrado— más una **migración sin listar** (`20260910_0005`), que en un deploy se traduce en una columna que el worker escribe y no existe. Un cambio de comportamiento, una migración o un límite que deja de ser cierto se documentan en su commit, en el archivo de su dominio en `docs/claude/` (ver el índice de arriba); si el cambio no merece una línea ahí, no merece un bullet aparte después. Este archivo sólo recibe una línea de índice cuando aparece un tema nuevo o una regla que no se puede romper: tiene que seguir bajo 40.000 caracteres, o Claude Code lo carga entero en cada sesión y avisa que degrada el rendimiento.
- **El backend NO está normalizado con `ruff format`.** No correr `ruff format` ni `make format` durante un cambio funcional: como ningún archivo pasó por el formateador, tocar uno solo lo reformatea entero y mete cientos de líneas de ruido ajeno en el diff — se pierde la revisión, se esconden regresiones y revertir el commit revierte cosas que nadie quiso tocar. El CI exige **`ruff check .`**, nunca `ruff format --check`. `make format` falla a propósito y explica esto; `make fix` corre `ruff check --fix` (inspeccionar el diff antes de commitear). La normalización global es un trabajo aparte: cerrar/pausar las ramas activas → `make format-normalize-global` → **un commit exclusivo sin cambios funcionales** → recién ahí agregar `ruff format --check .` al CI y avisar para que rebasen. Antes de cada commit: `git diff --stat` y `git diff --check`.
- Tipos estrictos (`mypy strict=true`). Cada endpoint necesita schema Pydantic.
- `tenant_id` del JWT en CADA query de negocio — nunca del cliente.
- Scores recalculan solo ante cambios de datos (Celery async).
- Toda decisión → `decision_audit_log` (insert-only).
- Fail-closed en writes sensibles.
- `ActionType` cerrado (31 valores) — cambiar requiere actualizar `RiskEngine` y tests.
- System prompts: heurísticas como valores numéricos, nunca texto narrativo.
- Todo input de usuario a LLM pasa por `wrap_user_input()`.
- Toda aritmética financiera va por un servicio determinístico — LLMs nunca calculan montos. Métricas de negocio: `FactsService` (única fuente de verdad). Aritmética puntual del chat: `DeterministicFinance` (hasta migrar a FactsService).
- Custom fields no se validan en write time (MVP). Agregar validación en `field_definition_service.validate_custom_fields()` cuando sea necesario.
- **No-invention rule:** ningún componente del dashboard, agente LLM, ni job de background puede mostrar análisis, scores, narrativas, alertas o conclusiones cuando `confidence_level == "LOW"` (`data_completeness_score < 50`). La UI muestra un empty state solicitando los datos faltantes. Los jobs no persisten `Insight`, `ActionSuggestion` ni notificaciones analíticas con `confidence_level == "LOW"`. Nunca reemplazar scores `None`/`0` con defaults neutrales (`or 70`, `or 50`, etc.): si falta un componente, el score real es 0 o ausente — bajar la confianza, no maquillarlo. La compuerta sólo sirve si `data_completeness_score` mide datos reales y no respuestas de formulario: ver invariante 5b.

---

## Tests

- pytest + pytest-asyncio (`asyncio_mode = "auto"`). DB en memoria: SQLite + aiosqlite.
- Cobertura: `make test` no la mide (default rápido); `make test-cov` exige **60%**, igual que CI.
- `pytest app/tests/domain/test_health_score.py -v --no-cov`

## CI

- `ci-backend.yml`: ruff + mypy + pytest (cov ≥ 60%) + Docker build. Triggers `backend/**` → `main/develop`.
- `ci-frontend.yml`: tsc + ESLint + `next build`. Triggers `frontend/**` → `main/develop`.
- Cupo de minutos agotado = jobs en `failure` en 3-4 s sin correr nada. Variable `CI_RUNNER=self-hosted` → runner propio en la Mac (gratis). Detalle: [`docs/claude/ci.md`](docs/claude/ci.md).

## Deploy (Railway + Vercel — beta)

| Servicio | Manifiesto | Start |
|----------|-----------|-------|
| `vektor-api` | `backend/railway.toml` | `sh scripts/start.sh` → uvicorn |
| `vektor-worker` | `backend/worker/railway.toml` | `sh scripts/start_worker.sh` → Celery |
| `vektor-mcp` | (servicio MCP propio) | — |
| Postgres | Neon (externo) | — |
| Redis | Railway managed | — |
| Frontend | Vercel, root `frontend/` | `next start` |

**Migraciones automáticas (Railway pre-deploy):** `backend/railway.toml` define `preDeployCommand = "sh scripts/migrate.sh"`, que corre UNA vez por deploy del servicio `vektor-api`, en un contenedor one-off con `DATABASE_URL` de Neon, ANTES de que la nueva versión reciba tráfico. Tres pasos, en orden: **(1)** `scripts/migrate_preflight.py` — diagnóstico; **(2)** `alembic upgrade head`; **(3)** `scripts/seed_vertical_fields.py` (upsert idempotente de las definiciones de campos por rubro; va después porque un rubro nuevo puede depender de una migración de este mismo deploy). Si (2) o (3) fallan, `set -e` corta y Railway aborta el deploy: la versión vieja sigue sirviendo (fail-safe). El `startCommand` (`start_web.sh`) ya NO corre Alembic — solo uvicorn. `make migrate-neon` queda para aplicar manualmente desde el shell (local contra Neon) cuando haga falta fuera de un deploy.

**Preflight de migración** (`backend/scripts/migrate_preflight.py`): imprime en el log del deploy **contra qué base se va a migrar** — de qué variable salió la URL (`DATABASE_URL_SYNC` le gana a `DATABASE_URL`), host/puerto/base (nunca usuario ni contraseña), `current_database`/`current_user`/`current_schema`/`search_path`, y TODAS las tablas `alembic_version` visibles con sus filas (avisa si hay más de una: leer la versión de un schema y escribir el DDL en otro produce `DuplicateColumn` en loop). Resuelve la URL con el MISMO código que `migrations/env.py` — `app/persistence/db/alembic_url.py`, extraído justamente para que los dos no puedan divergir; si el preflight resolviera por su cuenta podría reportar sobre una base distinta de la que alembic toca. **Nunca corta el deploy:** ante cualquier error sale con 0 y la autoridad sigue siendo alembic. Existe porque un esquema que no coincide con el `alembic_version` que uno ve desde su shell es indistinguible de "otra base", "otro schema" o "log viejo" sin este dato.

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
