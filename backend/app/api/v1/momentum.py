"""Momentum engine endpoint."""

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import get_current_tenant
from app.persistence.db.session import get_db_session
from app.persistence.models.business import MomentumProfile
from app.persistence.models.score import WeeklyScoreHistory
from app.persistence.models.tenant import Tenant
from app.schemas.momentum import (
    ActiveGoalResponse,
    MilestoneItem,
    MomentumProfileResponse,
    WeeklyHistoryItem,
)

router = APIRouter()


@router.get(
    "/profile",
    response_model=MomentumProfileResponse,
    summary="Momentum profile for the current tenant",
)
async def get_momentum_profile(
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db_session),
) -> MomentumProfileResponse:
    """
    Returns the tenant's momentum profile including:
    - best score ever and date
    - current active goal (weakest dimension)
    - unlocked milestones
    - estimated value protected (ARS)
    - improving streak in weeks
    - last 8 weeks of score history
    """
    mp_q = await session.execute(
        select(MomentumProfile).where(MomentumProfile.tenant_id == tenant.tenant_id)
    )
    mp: MomentumProfile | None = mp_q.scalar_one_or_none()

    if mp is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No momentum profile found. Run the weekly job first.",
        )

    # Last 8 weeks of history
    history_q = await session.execute(
        select(WeeklyScoreHistory)
        .where(WeeklyScoreHistory.tenant_id == tenant.tenant_id)
        .order_by(WeeklyScoreHistory.week_start.desc())
        .limit(8)
    )
    history_rows = list(history_q.scalars().all())
    history_rows.sort(key=lambda r: r.week_start)  # ascending for chart

    weekly_history = [
        WeeklyHistoryItem(
            week_start=row.week_start,
            week_end=row.week_end,
            avg_score=float(row.avg_score),
            delta=float(row.delta) if row.delta is not None else None,
            trend_label=row.trend_label,
        )
        for row in history_rows
    ]

    # Active goal
    active_goal: ActiveGoalResponse | None = None
    if mp.active_goal_json:
        g = mp.active_goal_json
        active_goal = ActiveGoalResponse(
            weak_dimension=g.get("weak_dimension", ""),
            goal=g.get("goal", ""),
            action=g.get("action", ""),
            estimated_delta=g.get("estimated_delta", 0),
            estimated_weeks=g.get("estimated_weeks", 0),
        )

    # Milestones
    milestones: list[MilestoneItem] = [
        MilestoneItem(
            code=m["code"],
            label=m["label"],
            unlocked_at=datetime.fromisoformat(m["unlocked_at"]),
        )
        for m in (mp.milestones_json or [])
    ]

    return MomentumProfileResponse(
        best_score_ever=mp.best_score_ever,
        best_score_date=mp.best_score_date,
        active_goal=active_goal,
        milestones_unlocked=milestones,
        estimated_value_protected_ars=float(mp.estimated_value_protected_ars or 0),
        improving_streak_weeks=mp.improving_streak_weeks or 0,
        weekly_history=weekly_history,
    )
