"""Insights and action suggestions endpoints."""

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import get_current_tenant
from app.persistence.db.session import get_db_session
from app.persistence.models.business import ActionSuggestion, Insight
from app.persistence.models.tenant import Tenant
from app.schemas.common import MessageResponse

router = APIRouter()


# ── Schemas ───────────────────────────────────────────────────────────────────


class InsightResponse(BaseModel):
    model_config = {"from_attributes": True}

    id: UUID
    title: str
    description: str
    insight_type: str
    severity_code: str
    heuristic_version: str
    created_at: datetime


class ActionSuggestionResponse(BaseModel):
    model_config = {"from_attributes": True}

    id: UUID
    title: str
    description: str
    action_type: str
    risk_level: str
    status: str
    created_at: datetime


class CurrentInsightResponse(BaseModel):
    insight: InsightResponse
    action_suggestion: ActionSuggestionResponse | None


# ── Endpoints ─────────────────────────────────────────────────────────────────


@router.get(
    "/current",
    response_model=CurrentInsightResponse,
    summary="Active insight + action suggestion for the current tenant",
)
async def get_current_insight(
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db_session),
) -> CurrentInsightResponse:
    """
    Returns the most recent Insight and its associated ActionSuggestion.
    Raises 404 if no insight has been generated yet.
    """
    insight_result = await session.execute(
        select(Insight)
        .where(Insight.tenant_id == tenant.tenant_id)
        .order_by(Insight.created_at.desc())
        .limit(1)
    )
    insight = insight_result.scalar_one_or_none()

    if insight is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No insight available yet.",
        )

    action_result = await session.execute(
        select(ActionSuggestion)
        .where(
            ActionSuggestion.tenant_id == tenant.tenant_id,
            ActionSuggestion.insight_id == insight.id,
        )
        .order_by(ActionSuggestion.created_at.desc())
        .limit(1)
    )
    action = action_result.scalar_one_or_none()

    return CurrentInsightResponse(
        insight=InsightResponse.model_validate(insight),
        action_suggestion=ActionSuggestionResponse.model_validate(action) if action else None,
    )


@router.get("", response_model=list[InsightResponse], summary="List insights")
async def list_insights(
    limit: int = Query(default=20, ge=1, le=100),
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db_session),
) -> list[InsightResponse]:
    q = (
        select(Insight)
        .where(Insight.tenant_id == tenant.tenant_id)
        .order_by(Insight.created_at.desc())
        .limit(limit)
    )
    result = await session.execute(q)
    return [InsightResponse.model_validate(i) for i in result.scalars().all()]


@router.get(
    "/actions",
    response_model=list[ActionSuggestionResponse],
    summary="List action suggestions",
)
async def list_actions(
    status_filter: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=20, ge=1, le=100),
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db_session),
) -> list[ActionSuggestionResponse]:
    q = select(ActionSuggestion).where(ActionSuggestion.tenant_id == tenant.tenant_id)
    if status_filter:
        q = q.where(ActionSuggestion.status == status_filter)
    q = q.order_by(ActionSuggestion.created_at.desc()).limit(limit)
    result = await session.execute(q)
    return [ActionSuggestionResponse.model_validate(a) for a in result.scalars().all()]
