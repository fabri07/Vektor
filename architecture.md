# Architecture

## Overview

Véktor es un **monolito modular multi-tenant** (v1) para salud financiera de PYMEs.

- Backend: FastAPI + SQLAlchemy + Alembic + PostgreSQL
- Async: Celery + Redis
- Frontend: Next.js 15 (App Router) + TypeScript + Tailwind
- Auth: JWT HS256 + RBAC propio
- Integrations: S3-compatible, SMTP

## Backend Layers

`backend/app/`

- `api/v1/`: endpoints REST por dominio
- `schemas/`: contratos Pydantic request/response
- `application/`: services, commands, queries, DTOs
- `domain/`: lógica pura (score, negocio)
- `state/`: Business State Layer (pre-procesa datos antes del engine)
- `heuristics/`: reglas por vertical y versiones
- `persistence/`: engine, session, models, repositories, migrations
- `jobs/`: workers de score/reportes/notificaciones
- `observability/`: logging y métricas

## Data Model (v1.1)

Tablas principales: `tenants`, `users`, `subscriptions`, `business_profiles`, `heuristic_rule_sets`, `business_snapshots`, `products`, `sales_entries`, `expense_entries`, `health_score_snapshots`, `decision_audit_log`, `momentum_profiles`, `weekly_score_history`, `insights`, `action_suggestions`, `uploaded_files`, `notifications`, `user_activity_events`, `stock_snapshots`.

Reglas clave:

- `tenant_id` enforced en queries de negocio
- auditoría de decisiones en `decision_audit_log`
- snapshots versionados para trazabilidad (`business_snapshots`, `health_score_snapshots`)

## Runtime Flow

1. Usuario opera en frontend (JWT).
2. API valida RBAC + tenant y persiste eventos/datos.
3. Cambios de datos disparan cálculo asíncrono de score (Celery).
4. Business State Layer consolida estado.
5. Health engine aplica ruleset activo y guarda snapshot/auditoría.
6. API expone score, tendencias, insights y acciones.

## Operational Principles

- Fail-closed en writes sensibles
- Recalcular score solo ante cambios de datos (no por request)
- Índices orientados a consultas por tenant y series temporales
- CI en GitHub Actions (lint, type-check, tests, build)
