"""Health score endpoints."""

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import get_current_tenant
from app.persistence.db.session import get_db_session
from app.persistence.models.tenant import Tenant
from app.persistence.repositories.health_score_repository import HealthScoreRepository
from app.schemas.health_score import (
    CalculatingResponse,
    HealthScoreResponse,
    HealthScoreV2Response,
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
    snapshot = await repo.get_latest(tenant.tenant_id)
    if snapshot is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No health score available yet. Add some sales or expenses to get started.",
        )
    dimensions: list[object] = snapshot.dimensions if isinstance(snapshot.dimensions, list) else []
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
    history = await repo.list_by_tenant(tenant.tenant_id, limit=2)
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
    snapshots = await repo.list_by_tenant(tenant.tenant_id, limit=limit)
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
    return await repo.get_weekly_history(tenant.tenant_id, limit=limit)  # type: ignore[return-value]


@router.get(
    "/latest",
    response_model=HealthScoreV2Response | CalculatingResponse,
    summary="Latest health score with explicit subscores (F1-01)",
)
async def get_latest_score(
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db_session),
) -> HealthScoreV2Response | CalculatingResponse:
    """
    Returns the most recent HealthScoreSnapshot with all F1-01 subscore columns.
    If no score has been computed yet, returns { "status": "CALCULATING" }.
    """
    repo = HealthScoreRepository(session)
    snapshot = await repo.get_latest(tenant.tenant_id)
    if snapshot is None or snapshot.score_cash is None:
        return CalculatingResponse()
    return HealthScoreV2Response(
        id=snapshot.id,
        tenant_id=snapshot.tenant_id,
        score_total=int(snapshot.total_score),
        score_cash=snapshot.score_cash,
        score_margin=snapshot.score_margin,
        score_stock=snapshot.score_stock,
        score_supplier=snapshot.score_supplier,
        primary_risk_code=snapshot.primary_risk_code or "",
        confidence_level=snapshot.confidence_level or "",
        data_completeness_score=float(snapshot.data_completeness_score or 0),
        level=snapshot.level,
        created_at=snapshot.created_at,
    )


@router.get(
    "/history/v2",
    response_model=list[HealthScoreV2Response],
    summary="Last 12 health scores with explicit subscores (F1-01)",
)
async def get_score_history_v2(
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db_session),
) -> list[HealthScoreV2Response]:
    """Returns the last 12 health score snapshots for trend charts."""
    repo = HealthScoreRepository(session)
    snapshots = await repo.list_by_tenant(tenant.tenant_id, limit=12)
    return [
        HealthScoreV2Response(
            id=s.id,
            tenant_id=s.tenant_id,
            score_total=int(s.total_score),
            score_cash=s.score_cash or 0,
            score_margin=s.score_margin or 0,
            score_stock=s.score_stock or 0,
            score_supplier=s.score_supplier or 0,
            primary_risk_code=s.primary_risk_code or "",
            confidence_level=s.confidence_level or "",
            data_completeness_score=float(s.data_completeness_score or 0),
            level=s.level,
            created_at=s.created_at,
        )
        for s in snapshots
        if s.score_cash is not None  # only F1-01 snapshots
    ]
