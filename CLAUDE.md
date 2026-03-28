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
docker-compose up --build

# Ejecutar tests
pytest

# Ejecutar un test específico
pytest app/tests/api/v1/test_auth.py::test_login -v

# Ejecutar tests sin cobertura (más rápido)
pytest --no-cov

# Linting y formato
ruff check app/
ruff format app/

# Type checking
mypy app/

# Migraciones
alembic upgrade head
alembic revision --autogenerate -m "descripcion"
alembic downgrade -1
```

### Frontend (correr desde `frontend/`)

```bash
npm run dev          # dev server en :3000
npm run build        # build de producción
npm run lint         # ESLint
npm run type-check   # tsc --noEmit
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

**Regla crítica:** Los datos crudos de transacciones NUNCA llegan al Health Engine directamente. Todo pasa por `BusinessStateLayer.compute()` primero, que normaliza el estado financiero en un `BusinessState` con scores por dimensión (0–100).

### Capas y responsabilidades

| Capa | Path | Responsabilidad |
|------|------|-----------------|
| API | `app/api/v1/` | Routing, validación Pydantic, auth deps |
| Deps | `app/api/v1/deps.py` | JWT decode, `get_current_user`, `get_current_tenant`, `require_role()` |
| Application | `app/application/services/` | Orquestación: llama BSL → Engine → Repo → Audit |
| Domain | `app/domain/` | Entidades puras Python: `HealthScore`, `BusinessProfile`, etc. |
| BSL | `app/state/business_state_layer.py` | Agrega revenue/expenses 30 días → 5 dimension scores |
| Heuristics | `app/heuristics/` | Reglas específicas por vertical (kiosco/decoracion/limpieza) |
| Persistence | `app/persistence/` | SQLAlchemy async, repositories, modelos, Alembic |
| Jobs | `app/jobs/` | Celery workers: scores, notifications, reports, ingestion (OCR, xlsx) |

### Autenticación y multi-tenancy

- JWT (HS256, python-jose). Payload: `sub` (user_id), `tenant_id`, `role_code`.
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

### Observabilidad

- Logging con `structlog`. Usar `get_logger(__name__)` en todos los módulos.
- `bind_request_context(tenant_id=..., user_id=...)` se llama en `deps.py` para cada request.
- Rate limiting con `slowapi` (200 req/min por defecto).

---

## Arquitectura del frontend

- Next.js 15 App Router. Rutas protegidas bajo `src/app/(protected)/`, públicas bajo `src/app/(public)/`.
- Estado global: Zustand (`src/stores/`). Server-state: TanStack Query.
- HTTP client: axios wrapper en `src/lib/api.ts` con `NEXT_PUBLIC_API_URL`.
- UI: Tailwind CSS + componentes en `src/components/ui/`. Sin librería de componentes externa.
- Validación de forms: Zod.
- Charts: Recharts.

---

## Reglas de trabajo

- **Mostrar plan antes de escribir código y esperar confirmación.**
- Tipos estrictos siempre. Cada endpoint necesita schema Pydantic de request y response.
- `tenant_id` enforced en CADA query de negocio, obtenido del JWT, nunca del cliente.
- Los scores se recalculan solo ante cambios de datos (Celery async), no en cada request.
- Todo decision generada se registra en `decision_audit_log` (insert-only, nunca update/delete).
- Fail-closed en cualquier write sensible: ante error, no continuar.

---

## Tests

- Framework: pytest + pytest-asyncio (`asyncio_mode = "auto"`).
- DB de tests: SQLite + aiosqlite en memoria (`conftest.py`).
- Cobertura mínima: 50% (`--cov-fail-under=50`).
- Correr un test de dominio: `pytest app/tests/domain/test_health_score.py -v --no-cov`

---

## Deploy

- Backend API: Railway, `backend/railway.toml` — `uvicorn ... --port $PORT`.
- Celery worker: Railway (servicio separado), `backend/worker/railway.toml`.
- Frontend: Vercel, root directory `frontend/`.
- `/health` — health check endpoint sin auth.
