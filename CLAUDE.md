# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

---

## Proyecto

Véktor es una plataforma SaaS de salud financiera para PYMEs argentinas (kioscos, decoración hogar, limpieza). Multi-tenant, monolito modular en v1.

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

# Migraciones
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
| Application | `app/application/services/` | Orquestación: llama BSL → Engine → Repo → Audit |
| **Agents** | `app/application/agents/` | Capa multiagente LLM (ver sección Agentes) |
| Domain | `app/domain/` | Entidades puras Python: `HealthScore`, `BusinessProfile`, etc. |
| BSL | `app/state/business_state_layer.py` | Agrega revenue/expenses 30 días → 5 dimension scores |
| Heuristics | `app/heuristics/` | Reglas específicas por vertical (kiosco/decoracion/limpieza) |
| Persistence | `app/persistence/` | SQLAlchemy async, repositories, modelos, Alembic |
| Jobs | `app/jobs/` | Celery workers: scores, notifications, reports, ingestion (OCR, xlsx) |
| Security | `app/application/security/prompt_defense.py` | `wrap_user_input()` — sanitiza input LLM contra prompt injection |

### API Routers (`app/api/v1/`)

Todos registrados en `router.py`. Dominios principales: `auth`, `oauth` (social login), `tenants`, `users`, `business_profiles`, `sales`, `expenses`, `products`, `health_scores`, `insights`, `momentum`, `notifications`, `files`, `ingestion`, `onboarding`, `agent` (LLM chat), `workspace` (Google Workspace), `admin`.

### Autenticación y multi-tenancy

- JWT (HS256, python-jose). Payload: `sub` (user_id), `tenant_id`, `role_code`.
- OAuth social login via `oauth.py` — identity tables: `user_auth_identity`, `user_google_workspace`.
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
- Benchmarks de margen por vertical (inyectar como valores numéricos): kiosco 18–28%, decoracion_hogar 30–45%, limpieza 20–35%.
- JSONs de heurística por rubro: `app/application/data/heuristics/{kiosco_almacen,limpieza,decoracion_hogar}.json`
- Para agregar un nuevo tipo: añadir entrada en `TEMPLATES`, agregar rama en `render_insight()`, y emitirlo desde el Health Engine.

### Capa de Agentes LLM (`app/application/agents/`)

6 agentes especializados coordinados por AgentCEO. El cliente NUNCA elige el agente destino.

| Agente | Context Budget | Estado | Responsabilidad |
|--------|---------------|--------|-----------------|
| AgentCEO | 2.000 tokens | ✅ Implementado | Router/coordinador, nunca accede a datos de negocio directamente |
| AgentCash | 3.000 tokens | 🔴 Stub (FASE-2) | Caja, ventas, cobros, pagos |
| AgentStock | 3.000 tokens | ✅ FASE-3B | Inventario, quiebres, rotación, merma (incluye Celery task) |
| AgentSupplier | 3.500 tokens | ✅ FASE-3C | Proveedores, filtrado de Gmail, generación de borradores |
| AgentHealth | 4.000 tokens | 🔴 Stub (FASE-2) | Score de salud, narrativa ejecutiva |
| AgentHelper | 2.500 tokens | 🔴 Stub (FASE-2) | FAQ, manual, guía funcional |

**Modelos LLM:**
- Clasificación / routing / extracción: `claude-haiku-4-5-20251001`
- Narrativa ejecutiva (AgentHealth, cuando se implemente): `claude-sonnet-4-6`

**Dependencia:** `anthropic` SDK — debe estar en `requirements.txt` con versión pinneada.

**Contratos fijos** (`app/application/agents/shared/schemas.py`):
- `AgentRequest`: `{ request_id, user_id, business_id, message, attachments, conversation_id }` — sin `agent_target`
- `AgentResponse`: `{ request_id, agent_name, status, risk_level, requires_approval, confidence, result, pending_action_id? }`
- `status`: `"success" | "requires_approval" | "requires_clarification" | "error"`
- `confidence`: `"HIGH" | "MEDIUM" | "LOW"` — nunca un float

**ActionType** (`shared/schemas.py`) — catálogo cerrado de 15 valores:

```
REGISTER_SALE          REGISTER_CASH_INFLOW    REGISTER_EXPENSE
REGISTER_PURCHASE      REGISTER_CASH_OUTFLOW   UPDATE_STOCK
REGISTER_STOCK_LOSS    CREATE_SUPPLIER_DRAFT   CREATE_PURCHASE_SUGGESTION
IMPORT_TABULAR_FILE    PARSE_DOCUMENT_FILE     GENERATE_HEALTH_REPORT
SYNC_TO_GOOGLE         CLASSIFY_GMAIL_MESSAGE  ANSWER_HELP_REQUEST
```

Nada fuera de esta lista puede ejecutarse. Agregar una acción requiere actualizar también `RiskEngine` y sus tests.

**RiskEngine** (`shared/risk_engine.py`) — función determinística pura, sin LLM. `HIGH` requiere aprobación; `MEDIUM` también; `LOW` no.

**ContextBuilder** (`shared/context_builder.py`) — respeta el budget por agente descartando secciones en este orden (primero en descartarse):
1. `historical_data` (400 tokens)
2. `conversation_history` (1.000 tokens)
3. `recent_events` (800 tokens)
4. `current_snapshot` (600 tokens) — SIEMPRE incluido hasta aquí
5. `business_heuristics` (300 tokens) — SIEMPRE incluido
6. `intent_and_entities` (200 tokens) — SIEMPRE incluido

**HeuristicEngine** (`shared/heuristic_engine.py`) — stub en FASE-1A, implementación completa en FASE-2B. Al inyectar heurísticas en el system prompt, usar valores **numéricos** (`{h.margin.min*100}%`), nunca texto narrativo.

**Prompt defense:** Todo input de usuario que llegue a un LLM debe pasar por `wrap_user_input()` de `app/application/security/prompt_defense.py` antes de incluirse en un prompt.

### Observabilidad

- Logging con `structlog`. Usar `get_logger(__name__)` en todos los módulos.
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

### Organización del frontend

| Directorio | Responsabilidad |
|------------|-----------------|
| `src/features/` | Módulos por feature: `auth`, `dashboard`, `onboarding`, `ingestion`, `notifications` |
| `src/services/` | Capa de llamadas HTTP por dominio: `auth`, `sales`, `expenses`, `products`, `health_score`, `dashboard`, `momentum`, `notifications`, `ingestion`, `onboarding` |
| `src/stores/` | Zustand: `authStore` (JWT + user), `toastStore` |
| `src/hooks/` | Custom hooks: `useAuth` |
| `src/types/api.ts` | Tipos TypeScript de respuestas de la API |
| `src/components/auth/AuthHydrationBoundary.tsx` | Hidrata auth desde localStorage antes de renderizar rutas protegidas |

---

## Reglas de trabajo

- **Mostrar plan antes de escribir código y esperar confirmación.**
- Tipos estrictos siempre (`mypy strict=true`). Cada endpoint necesita schema Pydantic de request y response.
- `tenant_id` enforced en CADA query de negocio, obtenido del JWT, nunca del cliente.
- Los scores se recalculan solo ante cambios de datos (Celery async), no en cada request.
- Todo decision generada se registra en `decision_audit_log` (insert-only, nunca update/delete).
- Fail-closed en cualquier write sensible: ante error, no continuar.
- En la capa de agentes: el catálogo de `ActionType` es cerrado — no agregar acciones fuera de los 15 definidos sin actualizar el `RiskEngine` y los tests.
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

- Backend API: Railway, `backend/railway.toml` — `uvicorn ... --port $PORT`.
- Celery worker: Railway (servicio separado), `backend/worker/railway.toml`.
- Frontend: Vercel, root directory `frontend/`.
- `/health` — health check endpoint sin auth.

## Demo

Acceso: `http://localhost:3000/demo` (password `Demo1234!` para todos):

| Email | Vertical | Score | Estado |
|-------|----------|-------|--------|
| demo.kiosco@vektor.app | Kiosco | 74 | Saludable |
| demo.limpieza@vektor.app | Limpieza | 51 | En riesgo |
| demo.deco@vektor.app | Decoración | 62 | Estable |

Cada tenant incluye 8 semanas de historial, momentum profile, insights, 8–15 productos y 30 días de transacciones. Regenerar con `make reset-demo`.
