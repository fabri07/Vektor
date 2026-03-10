"""Insights and action suggestions endpoints."""

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


class InsightResponse(BaseModel):
    model_config = {"from_attributes": True}

    id: UUID
    title: str
    body: str
    category: str
    severity: str
    is_read: bool


class ActionSuggestionResponse(BaseModel):
    model_config = {"from_attributes": True}

    id: UUID
    title: str
    description: str
    priority: str
    status: str


@router.get("", response_model=list[InsightResponse], summary="List insights")
async def list_insights(
    is_read: bool | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db_session),
) -> list[Insight]:
    q = select(Insight).where(Insight.tenant_id == tenant.id)
    if is_read is not None:
        q = q.where(Insight.is_read == is_read)
    q = q.order_by(Insight.created_at.desc()).limit(limit)
    result = await session.execute(q)
    return list(result.scalars().all())


@router.post("/{insight_id}/read", response_model=MessageResponse, summary="Mark insight as read")
async def mark_insight_read(
    insight_id: UUID,
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db_session),
) -> MessageResponse:
    result = await session.execute(
        select(Insight).where(Insight.id == insight_id, Insight.tenant_id == tenant.id)
    )
    insight = result.scalar_one_or_none()
    if not insight:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Insight not found.")
    insight.is_read = True
    session.add(insight)
    await session.flush()
    return MessageResponse(message="Insight marked as read.")


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
) -> list[ActionSuggestion]:
    q = select(ActionSuggestion).where(ActionSuggestion.tenant_id == tenant.id)
    if status_filter:
        q = q.where(ActionSuggestion.status == status_filter)
    q = q.order_by(ActionSuggestion.created_at.desc()).limit(limit)
    result = await session.execute(q)
    return list(result.scalars().all())
