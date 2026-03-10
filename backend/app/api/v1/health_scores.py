"""Health score endpoints."""

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import get_current_tenant
from app.persistence.db.session import get_db_session
from app.persistence.models.tenant import Tenant
from app.persistence.repositories.health_score_repository import HealthScoreRepository
from app.schemas.health_score import (
    HealthScoreResponse,
    ScoreSummaryResponse,
    WeeklyScoreHistoryResponse,
)

router = APIRouter()


@router.get(
    "/current",
    response_model=HealthScoreResponse,
    summary="Get the latest health score for the current tenant",
)
async def get_current_score(
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db_session),
) -> HealthScoreResponse:
    repo = HealthScoreRepository(session)
    snapshot = await repo.get_latest(tenant.id)
    if snapshot is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No health score available yet. Add some sales or expenses to get started.",
        )
    dimensions = snapshot.dimensions if isinstance(snapshot.dimensions, list) else []
    return HealthScoreResponse(
        id=snapshot.id,
        tenant_id=snapshot.tenant_id,
        total_score=snapshot.total_score,
        level=snapshot.level,
        dimensions=dimensions,
        triggered_by=snapshot.triggered_by,
        snapshot_date=snapshot.snapshot_date,
        created_at=snapshot.created_at,
    )


@router.get(
    "/summary",
    response_model=ScoreSummaryResponse,
    summary="Lightweight score summary for dashboard",
)
async def get_score_summary(
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db_session),
) -> ScoreSummaryResponse:
    repo = HealthScoreRepository(session)
    history = await repo.list_by_tenant(tenant.id, limit=2)
    if not history:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No health score available yet.",
        )
    current = history[0]
    previous = history[1] if len(history) > 1 else None
    delta = (current.total_score - previous.total_score) if previous else None
    return ScoreSummaryResponse(
        current_score=current.total_score,
        level=current.level,
        previous_score=previous.total_score if previous else None,
        delta=delta,
        snapshot_date=current.snapshot_date,
        needs_attention=current.level in ("critical", "warning"),
    )


@router.get(
    "/history",
    response_model=list[HealthScoreResponse],
    summary="Get historical health scores",
)
async def get_score_history(
    limit: int = Query(default=30, ge=1, le=90),
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db_session),
) -> list[HealthScoreResponse]:
    repo = HealthScoreRepository(session)
    snapshots = await repo.list_by_tenant(tenant.id, limit=limit)
    return [
        HealthScoreResponse(
            id=s.id,
            tenant_id=s.tenant_id,
            total_score=s.total_score,
            level=s.level,
            dimensions=s.dimensions if isinstance(s.dimensions, list) else [],
            triggered_by=s.triggered_by,
            snapshot_date=s.snapshot_date,
            created_at=s.created_at,
        )
        for s in snapshots
    ]


@router.get(
    "/weekly",
    response_model=list[WeeklyScoreHistoryResponse],
    summary="Weekly score history for trend charts",
)
async def get_weekly_history(
    limit: int = Query(default=12, ge=1, le=52),
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db_session),
) -> list[WeeklyScoreHistoryResponse]:
    repo = HealthScoreRepository(session)
    return await repo.get_weekly_history(tenant.id, limit=limit)  # type: ignore[return-value]
