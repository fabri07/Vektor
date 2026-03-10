Estás desarrollando Véktor, una plataforma SaaS de salud financiera
para PYMEs argentinas (kioscos, decoración hogar, limpieza).

STACK:
  Backend:  FastAPI (Python) + PostgreSQL + Redis + Celery
  Frontend: Next.js (App Router) + TypeScript + Tailwind CSS
  Auth:     JWT + RBAC propio
  Infra:    Docker + docker-compose
  Storage:  S3 compatible

PRINCIPIOS DE ARQUITECTURA:
  - Monolito modular en v1. Sin microservicios.
  - Fail-closed en cualquier write sensible.
  - tenant_id enforced en CADA query de negocio.
  - Todo cálculo pasa por Business State Layer antes del Health Engine.
  - Scores se recalculan solo ante cambios de datos (no en cada request).
  - Todo decision generada se registra en decision_audit_log.

ESTRUCTURA DE REPOS:
  backend/app/api/v1/       — endpoints REST
  backend/app/domain/       — lógica de negocio pura
  backend/app/application/  — services, commands, queries
  backend/app/persistence/  — repositories, models, migrations
  backend/app/jobs/         — workers Celery
  backend/app/heuristics/   — reglas por vertical
  backend/app/state/        — Business State Layer
  frontend/src/app/         — rutas Next.js (App Router)
  frontend/src/features/    — módulos por dominio
  frontend/src/components/  — componentes compartidos

MODELOS CLAVE DE BD (PostgreSQL):
  tenants, users, subscriptions, business_profiles,
  business_snapshots, heuristic_rule_sets, decision_audit_log,
  health_score_snapshots, momentum_profiles, weekly_score_history,
  products, sales_entries, expense_entries, insights,
  action_suggestions, uploaded_files, notifications

REGLA: Antes de escribir código, mostrá el plan de implementación
y esperá confirmación. Usá tipos estrictos siempre.
Cada endpoint debe tener su schema Pydantic de request y response.
