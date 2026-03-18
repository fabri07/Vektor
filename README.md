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

## Estructura del repositorio

```
Vektor/
├── backend/          # FastAPI app
│   ├── app/
│   │   ├── api/v1/       # Endpoints REST
│   │   ├── domain/       # Lógica de negocio pura
│   │   ├── application/  # Services, commands, queries
│   │   ├── persistence/  # Repositories, models, migrations
│   │   ├── jobs/         # Workers Celery
│   │   ├── heuristics/   # Reglas por vertical
│   │   └── state/        # Business State Layer
│   ├── Dockerfile
│   ├── pyproject.toml
│   └── requirements*.txt
├── frontend/         # Next.js app
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

## Arranque local

### Backend

```bash
cd backend
cp .env.example .env          # completar variables
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
alembic upgrade head
uvicorn app.main:app --reload
```

Si no tenés `python3.12` disponible localmente, usá Docker Compose para el backend.

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
