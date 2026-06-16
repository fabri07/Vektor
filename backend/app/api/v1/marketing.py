"""Endpoints de marketing: CRUD de métricas de redes (carga manual) + dashboard."""

from datetime import UTC, date, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import get_current_tenant, require_role
from app.application.services.marketing_service import MarketingService
from app.persistence.db.session import get_db_session
from app.persistence.models.audit import DecisionAuditLog
from app.persistence.models.social_metric import SocialMetric
from app.persistence.models.tenant import Tenant
from app.persistence.models.user import User
from app.persistence.repositories.social_metric_repository import SocialMetricRepository
from app.schemas.common import MessageResponse
from app.schemas.marketing import (
    CreateSocialMetricRequest,
    MarketingDashboardResponse,
    SocialMetricResponse,
    UpdateSocialMetricRequest,
)

router = APIRouter()


def _audit_metric_change(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    user_id: UUID,
    decision_type: str,
    metric_id: UUID,
    platform: str,
) -> None:
    session.add(
        DecisionAuditLog(
            tenant_id=tenant_id,
            decision_type=decision_type,
            decision_data={
                "record_type": "social_metric",
                "record_id": str(metric_id),
                "platform": platform,
                "source": "ui",
            },
            triggered_by="ui:marketing",
            actor_user_id=user_id,
            context={"endpoint": "marketing"},
            created_at=datetime.now(UTC),
        )
    )


@router.post(
    "/metrics",
    response_model=SocialMetricResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Cargar una métrica de red social",
)
async def create_metric(
    body: CreateSocialMetricRequest,
    tenant: Tenant = Depends(get_current_tenant),
    user: User = Depends(require_role("OWNER", "ADMIN")),
    session: AsyncSession = Depends(get_db_session),
) -> SocialMetric:
    repo = SocialMetricRepository(session)
    metric = SocialMetric(tenant_id=tenant.tenant_id, **body.model_dump())
    saved = await repo.save(metric)
    _audit_metric_change(
        session,
        tenant_id=tenant.tenant_id,
        user_id=user.user_id,
        decision_type="MARKETING_METRIC_CREATED",
        metric_id=saved.id,
        platform=saved.platform,
    )
    return saved


@router.post(
    "/metrics/bulk",
    response_model=list[SocialMetricResponse],
    status_code=status.HTTP_201_CREATED,
    summary="Cargar varias métricas de red social",
)
async def create_metrics_bulk(
    body: list[CreateSocialMetricRequest],
    tenant: Tenant = Depends(get_current_tenant),
    user: User = Depends(require_role("OWNER", "ADMIN")),
    session: AsyncSession = Depends(get_db_session),
) -> list[SocialMetric]:
    repo = SocialMetricRepository(session)
    saved: list[SocialMetric] = []
    for item in body:
        metric = SocialMetric(tenant_id=tenant.tenant_id, **item.model_dump())
        s = await repo.save(metric)
        _audit_metric_change(
            session,
            tenant_id=tenant.tenant_id,
            user_id=user.user_id,
            decision_type="MARKETING_METRIC_CREATED",
            metric_id=s.id,
            platform=s.platform,
        )
        saved.append(s)
    return saved


@router.get(
    "/metrics",
    response_model=list[SocialMetricResponse],
    summary="Listar métricas de redes",
)
async def list_metrics(
    platform: str | None = Query(default=None),
    from_date: date | None = Query(default=None),
    to_date: date | None = Query(default=None),
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db_session),
) -> list[SocialMetric]:
    repo = SocialMetricRepository(session)
    return await repo.list_by_tenant(
        tenant.tenant_id, platform=platform, from_date=from_date, to_date=to_date
    )


@router.patch(
    "/metrics/{metric_id}",
    response_model=SocialMetricResponse,
    summary="Actualizar una métrica de red social",
)
async def update_metric(
    metric_id: UUID,
    body: UpdateSocialMetricRequest,
    tenant: Tenant = Depends(get_current_tenant),
    user: User = Depends(require_role("OWNER", "ADMIN")),
    session: AsyncSession = Depends(get_db_session),
) -> SocialMetric:
    repo = SocialMetricRepository(session)
    metric = await repo.get_by_id(metric_id, tenant.tenant_id)
    if metric is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Metric not found.")
    updates = body.model_dump(exclude_unset=True)
    for field, value in updates.items():
        setattr(metric, field, value)
    saved = await repo.save(metric)
    _audit_metric_change(
        session,
        tenant_id=tenant.tenant_id,
        user_id=user.user_id,
        decision_type="MARKETING_METRIC_UPDATED",
        metric_id=saved.id,
        platform=saved.platform,
    )
    return saved


@router.delete(
    "/metrics/{metric_id}",
    response_model=MessageResponse,
    summary="Eliminar una métrica de red social",
)
async def delete_metric(
    metric_id: UUID,
    tenant: Tenant = Depends(get_current_tenant),
    user: User = Depends(require_role("OWNER", "ADMIN")),
    session: AsyncSession = Depends(get_db_session),
) -> MessageResponse:
    repo = SocialMetricRepository(session)
    metric = await repo.get_by_id(metric_id, tenant.tenant_id)
    if metric is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Metric not found.")
    _audit_metric_change(
        session,
        tenant_id=tenant.tenant_id,
        user_id=user.user_id,
        decision_type="MARKETING_METRIC_DELETED",
        metric_id=metric.id,
        platform=metric.platform,
    )
    await repo.delete(metric)
    return MessageResponse(message="Metric deleted.")


@router.get(
    "/dashboard",
    response_model=MarketingDashboardResponse,
    summary="Dashboard unificado de redes + ads vs ventas",
)
async def marketing_dashboard(
    days: int = Query(default=30, ge=1, le=365),
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db_session),
) -> MarketingDashboardResponse:
    service = MarketingService(session)
    return await service.get_dashboard(tenant.tenant_id, days=days)
