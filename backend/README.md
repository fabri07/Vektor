# Véktor Backend

Plataforma SaaS de salud financiera para PYMEs argentinas.
Stack: **FastAPI · PostgreSQL · Redis · Celery · Python 3.12**

## Quick start

```bash
# 1. Copiar variables de entorno
cp .env.example .env
# Editar .env con tus credenciales

# 2. Levantar servicios
make dev

# 3. Aplicar migraciones
make migrate

# 4. Ver docs
open http://localhost:8000/docs
```

## Comandos útiles

| Comando | Descripción |
|---|---|
| `make dev` | Levanta docker-compose con hot reload |
| `make test` | Corre tests con cobertura |
| `make migrate` | Aplica migraciones pendientes |
| `make migrate-create MSG="..."` | Crea nueva migración |
| `make lint` | Corre ruff linter |
| `make format` | Auto-formatea con ruff |
| `make typecheck` | Corre mypy |

## Arquitectura

```
app/
├── api/v1/          — Endpoints REST (un archivo por dominio)
├── domain/          — Entidades y value objects puros
├── application/     — Services, commands, queries, DTOs
├── persistence/     — DB engine, modelos SQLAlchemy, repositorios
├── schemas/         — Pydantic v2 request/response
├── jobs/            — Workers Celery
├── heuristics/      — Reglas por vertical (kiosco, decoracion, limpieza)
├── state/           — Business State Layer
├── integrations/    — S3, SMTP
├── observability/   — structlog, métricas
└── utils/           — JWT, paginación, helpers
```

## Principios

- **tenant_id enforced** en cada query de negocio
- **Fail-closed** en cualquier write sensible
- **Scores** se recalculan solo ante cambios de datos (Celery async)
- **Todo cálculo** pasa por Business State Layer antes del Health Engine
- **Toda decisión** se registra en `decision_audit_log`
