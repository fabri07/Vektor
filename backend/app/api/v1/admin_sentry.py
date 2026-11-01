"""
Errores de Sentry — SUPERADMIN.

GET /api/v1/admin/sentry/issues — issues sin resolver (14 días) de los proyectos de Véktor.

Sólo lectura. No usa el `tenant_id` del JWT: es información de la plataforma.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from redis.asyncio import Redis

from app.api.v1.deps import require_role
from app.application.services import sentry_issues_service as svc
from app.persistence.db.redis_client import get_redis
from app.schemas.admin_sentry import SentryIssuesResponse

router = APIRouter(dependencies=[Depends(require_role("SUPERADMIN"))])


@router.get("/issues", response_model=SentryIssuesResponse, summary="Errores recientes de Sentry")
async def get_issues(redis: Redis = Depends(get_redis)) -> SentryIssuesResponse:
    try:
        return await svc.get_issues(redis)
    except svc.SentryUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"code": exc.code, "message": "No se pudo consultar Sentry."},
        ) from exc
