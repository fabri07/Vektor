# Véktor

Plataforma SaaS de salud financiera para PYMEs argentinas.

## Stack

| Capa | Tecnología |
|------|------------|
| Backend | FastAPI (Python 3.12) + PostgreSQL + Redis + Celery |
| Frontend | Next.js 15 (App Router) + TypeScript + Tailwind CSS |
| Auth | JWT HS256 + RBAC propio |
| Infra | Docker + docker-compose |
| Storage | S3-compatible (boto3) |

---

## Demo

Levantá el stack completo con datos de demo precargados:

```bash
# 1. Levantar servicios
cd backend
docker compose -f docker-compose.yml up --build -d

# 2. Correr migraciones
make migrate

# 3. Cargar los 3 tenants demo
make seed-demo

# 4. Abrir el selector de demo
open http://localhost:3000/demo
```

**3 tenants demo — password: `Demo1234!`**

| Tenant | Email | Score | Estado | Riesgo principal |
|--------|-------|-------|--------|-----------------|
| Kiosco San Martín | `demo.kiosco@vektor.app` | 74 | Saludable | SUPPLIER_DEPENDENCY |
| Distribuidora Clean | `demo.limpieza@vektor.app` | 51 | En riesgo | CASH_LOW |
| Casa & Deco Palermo | `demo.deco@vektor.app` | 62 | Estable | MARGIN_LOW |

**Datos incluidos por tenant:**
- 8 semanas de historial de scores con tendencia realista
- Momentum profile completo (hitos, streak, valor protegido)
- Insights y acciones sugeridas calibradas por rubro
- 8–15 productos con costo, precio y stock
- 30 días de ventas y gastos
- 3 notificaciones no leídas

Para resetear y regenerar (útil entre demos a inversores):

```bash
make reset-demo
```

---

## Development

### Prerrequisitos

- Python 3.12
- Node.js 20+
- Docker + Docker Compose
- PostgreSQL 15 (o usar docker-compose)
- Redis 7 (o usar docker-compose)

### Backend

```bash
cd backend
cp .env.example .env          # completar variables
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
alembic upgrade head
uvicorn app.main:app --reload
```

Si no tenés Python 3.12 localmente, usá Docker Compose.

### Frontend

```bash
cd frontend
cp .env.local.example .env.local   # ajustar NEXT_PUBLIC_API_URL
npm install
npm run dev
```

### Docker Compose (stack completo)

```bash
docker compose -f backend/docker-compose.yml up --build
```

### Variables de entorno clave

| Variable | Default | Descripción |
|----------|---------|-------------|
| `ENVIRONMENT` | `development` | `development` / `production` |
| `DEBUG` | `false` | Activa logs verbose y deshabilita verificación de email |
| `DEMO_MODE` | `false` | Activa modo demo (omite verificación de email) |
| `DEMO_EMAIL` | `demo@vektor.app` | Email del usuario demo |
| `DEMO_PASSWORD` | `demo1234!` | Contraseña del usuario demo |
| `DATABASE_URL` | *(computed)* | Construida desde `POSTGRES_*` vars |

### Comandos útiles

```bash
make dev          # Docker Compose con hot reload
make test         # pytest con cobertura
make lint         # ruff check
make typecheck    # mypy
make seed-demo    # Cargar datos de demo
make reset-demo   # Resetear y recargar datos de demo
make migrate      # Correr migraciones pendientes
make migrate-create MSG="descripcion"  # Nueva migración
```

---

## Architecture

### Flujo principal

```
HTTP Request
     │
     ▼
┌────────────────────────────────────────┐
│           FastAPI (main.py)            │
│  ┌─────────────────────────────────┐   │
│  │  Middleware stack               │   │
│  │  • SlowAPI (rate limiter)       │   │
│  │  • CORS                         │   │
│  │  • Security headers             │   │
│  │  • Request logger (structlog)   │   │
│  └─────────────────────────────────┘   │
│               │                        │
│  ┌────────────▼──────────────────────┐ │
│  │  Router /api/v1/                  │ │
│  │  auth · tenants · users           │ │
│  │  sales · expenses · products      │ │
│  │  health-scores · insights         │ │
│  │  momentum · files · onboarding    │ │
│  │  notifications · ingestion        │ │
│  │  admin (SUPERADMIN only)          │ │
│  └────────────┬──────────────────────┘ │
└───────────────┼────────────────────────┘
                │
     ┌──────────▼──────────┐
     │   deps.py (JWT +     │
     │   RBAC + log ctx)    │
     └──────────┬──────────┘
                │
     ┌──────────▼──────────────────────┐
     │   Application Services          │
     │  ┌───────────────────────────┐  │
     │  │  Business State Layer     │  │
     │  │  (30-day aggregation)     │  │
     │  └───────────┬───────────────┘  │
     │              │                  │
     │  ┌───────────▼───────────────┐  │
     │  │  Health Engine            │  │
     │  │  Heuristics (kiosco /     │  │
     │  │  decoracion / limpieza)   │  │
     │  └───────────┬───────────────┘  │
     └──────────────┼──────────────────┘
                    │
     ┌──────────────▼──────────────────┐
     │   Persistence                   │
     │  PostgreSQL (async SQLAlchemy)  │
     │  + decision_audit_log (insert-  │
     │    only, all decisions logged)  │
     └──────────────┬──────────────────┘
                    │
          ┌─────────▼─────────┐
          │  Celery Workers   │
          │ (score recalc +   │
          │  notifications +  │
          │  weekly report)   │
          └───────────────────┘

Observability:
  structlog → stdout (JSON in prod, pretty in dev)
  Context per request: environment, method, endpoint, tenant_id, user_id
  Context per job: job_name, tenant_id, duration_ms, success/error
  user_activity_events table → onboarding funnel + job stats
  GET /api/v1/admin/metrics → SUPERADMIN dashboard
```

### Principios de arquitectura

- **Monolito modular** — sin microservicios en v1
- **Fail-closed** — cualquier write sensible falla de forma segura
- **tenant_id enforced** — en CADA query de negocio
- **Business State Layer** — corre antes del Health Engine en todo cálculo
- **Scores bajo demanda** — se recalculan solo ante cambios de datos (Celery async)
- **Insert-only audit log** — toda decisión generada se registra en `decision_audit_log`

---

## Estructura del repositorio

```
Vektor/
├── backend/
│   ├── app/
│   │   ├── api/v1/       # Endpoints REST
│   │   ├── domain/       # Lógica de negocio pura
│   │   ├── application/  # Services, commands, queries
│   │   ├── persistence/  # Repositories, models, migrations
│   │   ├── jobs/         # Workers Celery
│   │   ├── heuristics/   # Reglas por vertical
│   │   ├── state/        # Business State Layer
│   │   └── observability/ # structlog + metrics
│   ├── scripts/
│   │   └── seed_demo_data.py
│   ├── Makefile
│   ├── Dockerfile
│   ├── pyproject.toml
│   └── requirements*.txt
├── frontend/
│   ├── src/
│   │   ├── app/          # Rutas (App Router)
│   │   ├── components/   # Componentes compartidos
│   │   ├── features/     # Módulos por dominio
│   │   ├── hooks/
│   │   ├── lib/
│   │   ├── services/
│   │   ├── stores/
│   │   ├── types/
│   │   └── validation/
│   └── package.json
└── .github/
    └── workflows/
        ├── ci-backend.yml
        └── ci-frontend.yml
```

---

## CI/CD

Los workflows de GitHub Actions corren automáticamente en push y PR a `main` y `develop`.

### Backend (`ci-backend.yml`)

1. Ruff — linting
2. mypy — type checking (strict)
3. pytest — tests con cobertura mínima 60% (SQLite in-memory, sin servicios externos)
4. Docker build — verificación de imagen

### Frontend (`ci-frontend.yml`)

1. `tsc --noEmit` — type checking
2. `next lint` — ESLint
3. `next build` — build de producción

---

## Protección de rama `main`

> La configuración de Branch Protection **no se puede hacer por código** — debe realizarse
> manualmente en GitHub Settings una sola vez por repositorio.

### Pasos

1. Ir a **Settings → Branches** en el repositorio de GitHub.
2. Hacer clic en **"Add branch ruleset"** (o "Add rule" en la vista clásica).
3. En **Branch name pattern** escribir `main`.
4. Activar las siguientes opciones:

| Opción | Valor |
|--------|-------|
| Require a pull request before merging | ✅ |
| Require status checks to pass before merging | ✅ |
| Status checks requeridos | `CI — Backend / Lint · Type-check · Test · Docker build` y `CI — Frontend / Type-check · Lint · Build` |
| Require branches to be up to date before merging | ✅ |
| Do not allow bypassing the above settings | ✅ |
| Allow force pushes | ❌ (desactivado) |
| Allow deletions | ❌ (desactivado) |

> **Nota sobre los nombres de los checks:** Los nombres exactos que hay que ingresar en
> "Status checks" son los valores del campo `name` dentro de cada job en los archivos YAML.
> Aparecen en la pestaña **Actions** del repositorio después de la primera ejecución del workflow.

5. Guardar con **"Create"** (o **"Save changes"**).

A partir de ese momento, cualquier push directo a `main` será rechazado y todo merge requerirá
que ambos workflows de CI hayan pasado exitosamente.
