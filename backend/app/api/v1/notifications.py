"""Notification endpoints."""

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import get_current_tenant, get_current_user
from app.persistence.db.session import get_db_session
from app.persistence.models.notification import Notification
from app.persistence.models.tenant import Tenant
from app.persistence.models.user import User
from app.schemas.common import MessageResponse

router = APIRouter()


class NotificationItem(BaseModel):
    model_config = {"from_attributes": True}

    id: UUID
    title: str
    body: str
    notification_type: str
    channel: str
    is_read: bool
    created_at: datetime


class NotificationListResponse(BaseModel):
    notifications: list[NotificationItem]
    unread_count: int


class CreateNotificationRequest(BaseModel):
    user_id: UUID | None = None
    title: str
    body: str
    notification_type: str
    channel: str = "in_app"


@router.post(
    "",
    response_model=NotificationItem,
    status_code=status.HTTP_201_CREATED,
    summary="Create notification (internal)",
    include_in_schema=False,
)
async def create_notification(
    payload: CreateNotificationRequest,
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db_session),
) -> Notification:
    notification = Notification(
        tenant_id=tenant.id,
        user_id=payload.user_id,
        title=payload.title,
        body=payload.body,
        notification_type=payload.notification_type,
        channel=payload.channel,
        is_read=False,
    )
    session.add(notification)
    await session.flush()
    await session.refresh(notification)
    return notification


@router.get("", response_model=NotificationListResponse, summary="List my notifications")
async def list_notifications(
    is_read: bool | None = Query(default=None),
    limit: int = Query(default=30, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db_session),
) -> NotificationListResponse:
    base_q = select(Notification).where(
        Notification.tenant_id == tenant.id,
        Notification.user_id == current_user.id,
    )
    if is_read is not None:
        base_q = base_q.where(Notification.is_read == is_read)

    list_q = base_q.order_by(Notification.created_at.desc()).limit(limit)
    result = await session.execute(list_q)
    notifications = list(result.scalars().all())

    unread_result = await session.execute(
        select(func.count()).select_from(
            select(Notification).where(
                Notification.tenant_id == tenant.id,
                Notification.user_id == current_user.id,
                Notification.is_read.is_(False),
            ).subquery()
        )
    )
    unread_count: int = unread_result.scalar_one()

    return NotificationListResponse(
        notifications=[NotificationItem.model_validate(n) for n in notifications],
        unread_count=unread_count,
    )


@router.patch(
    "/{notification_id}/read",
    response_model=MessageResponse,
    summary="Mark notification as read",
)
async def mark_notification_read(
    notification_id: UUID,
    current_user: User = Depends(get_current_user),
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db_session),
) -> MessageResponse:
    result = await session.execute(
        select(Notification).where(
            Notification.id == notification_id,
            Notification.tenant_id == tenant.id,
            Notification.user_id == current_user.id,
        )
    )
    notification = result.scalar_one_or_none()
    if not notification:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Notification not found."
        )
    notification.is_read = True
    session.add(notification)
    await session.flush()
    return MessageResponse(message="Notification marked as read.")


@router.post("/read-all", response_model=MessageResponse, summary="Mark all notifications as read")
async def mark_all_read(
    current_user: User = Depends(get_current_user),
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db_session),
) -> MessageResponse:
    from sqlalchemy import update  # noqa: PLC0415

    await session.execute(
        update(Notification)
        .where(
            Notification.tenant_id == tenant.id,
            Notification.user_id == current_user.id,
            Notification.is_read.is_(False),
        )
        .values(is_read=True)
    )
    return MessageResponse(message="All notifications marked as read.")
