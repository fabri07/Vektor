"""
SUPERADMIN metrics endpoint.

GET /api/v1/admin/metrics
  Returns platform-wide aggregate stats. Requires role SUPERADMIN.
"""

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import require_role
from app.persistence.db.session import get_db_session
from app.persistence.models.activity import UserActivityEvent
from app.persistence.models.business import BusinessProfile, BusinessSnapshot
from app.persistence.models.score import HealthScoreSnapshot
from app.persistence.models.tenant import Tenant
from app.schemas.admin import AdminMetricsResponse, JobStats

router = APIRouter()


@router.get(
    "/metrics",
    response_model=AdminMetricsResponse,
    summary="Platform-wide metrics (SUPERADMIN only)",
    dependencies=[Depends(require_role("SUPERADMIN"))],
)
async def get_admin_metrics(
    session: AsyncSession = Depends(get_db_session),
) -> AdminMetricsResponse:
    # 1. total_tenants
    total_tenants: int = (
        await session.scalar(select(func.count()).select_from(Tenant))
    ) or 0

    # 2. total_onboarding_completed
    total_onboarding: int = (
        await session.scalar(
            select(func.count()).select_from(BusinessProfile).where(
                BusinessProfile.onboarding_completed.is_(True)
            )
        )
    ) or 0

    # 3. avg_data_completeness_score — latest snapshot per tenant
    latest_snapshot_subq = (
        select(
            BusinessSnapshot.tenant_id,
            func.max(BusinessSnapshot.snapshot_date).label("max_date"),
        )
        .group_by(BusinessSnapshot.tenant_id)
        .subquery()
    )
    avg_completeness: float | None = await session.scalar(
        select(func.avg(BusinessSnapshot.data_completeness_score)).where(
            BusinessSnapshot.tenant_id == latest_snapshot_subq.c.tenant_id,
            BusinessSnapshot.snapshot_date == latest_snapshot_subq.c.max_date,
        )
    )

    # 4. avg_health_score — latest snapshot per tenant
    latest_score_subq = (
        select(
            HealthScoreSnapshot.tenant_id,
            func.max(HealthScoreSnapshot.snapshot_date).label("max_date"),
        )
        .group_by(HealthScoreSnapshot.tenant_id)
        .subquery()
    )
    avg_health: float | None = await session.scalar(
        select(func.avg(HealthScoreSnapshot.total_score)).where(
            HealthScoreSnapshot.tenant_id == latest_score_subq.c.tenant_id,
            HealthScoreSnapshot.snapshot_date == latest_score_subq.c.max_date,
        )
    )

    # 5. jobs_last_24h
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    job_success: int = (
        await session.scalar(
            select(func.count()).select_from(UserActivityEvent).where(
                UserActivityEvent.event_type == "JOB_SUCCESS",
                UserActivityEvent.created_at >= cutoff,
            )
        )
    ) or 0
    job_failed: int = (
        await session.scalar(
            select(func.count()).select_from(UserActivityEvent).where(
                UserActivityEvent.event_type == "JOB_FAILED",
                UserActivityEvent.created_at >= cutoff,
            )
        )
    ) or 0

    # 6. tenants_by_vertical
    rows = (
        await session.execute(
            select(BusinessProfile.vertical_code, func.count().label("cnt"))
            .group_by(BusinessProfile.vertical_code)
        )
    ).all()
    tenants_by_vertical: dict[str, int] = {row.vertical_code: row.cnt for row in rows}

    return AdminMetricsResponse(
        total_tenants=total_tenants,
        total_onboarding_completed=total_onboarding,
        avg_data_completeness_score=(
            round(float(avg_completeness), 2) if avg_completeness is not None else None
        ),
        avg_health_score=(
            round(float(avg_health), 2) if avg_health is not None else None
        ),
        jobs_last_24h=JobStats(success=job_success, failed=job_failed),
        tenants_by_vertical=tenants_by_vertical,
    )
