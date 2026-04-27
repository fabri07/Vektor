"""Tests for /api/v1/notifications endpoints."""

import uuid
from datetime import UTC, datetime

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.jobs.generate_insight import create_health_action_notifications
from app.persistence.models.notification import Notification
from app.persistence.models.tenant import Tenant
from app.persistence.models.user import User


async def _create_notification(
    session: AsyncSession,
    tenant: Tenant,
    user: User,
    title: str = "Test",
    notification_type: str = "milestone",
    is_read: bool = False,
) -> Notification:
    n = Notification(
        tenant_id=tenant.tenant_id,
        user_id=user.user_id,
        title=title,
        body="Test body",
        notification_type=notification_type,
        channel="in_app",
        is_read=is_read,
    )
    session.add(n)
    await session.commit()
    await session.refresh(n)
    return n


@pytest.mark.asyncio
class TestNotificationCreatedOnMilestone:
    """Test that notifications are created (simulating what update_momentum does)."""

    async def test_notification_created_on_milestone(
        self,
        db_session: AsyncSession,
        sample_tenant: Tenant,
        sample_user: User,
        client: AsyncClient,
        auth_headers: dict,
    ) -> None:
        # Simulate milestone notification created by momentum job
        await _create_notification(
            db_session,
            sample_tenant,
            sample_user,
            title="¡Hito desbloqueado: Primera semana de mejora!",
            notification_type="milestone",
        )

        resp = await client.get("/api/v1/notifications", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["unread_count"] == 1
        notifs = data["notifications"]
        assert len(notifs) == 1
        assert notifs[0]["notification_type"] == "milestone"
        assert notifs[0]["is_read"] is False
        assert notifs[0]["action_url"] is None
        assert "created_at" in notifs[0]

    async def test_health_action_notification_created_for_owner(
        self,
        db_session: AsyncSession,
        sample_tenant: Tenant,
        sample_user: User,
        client: AsyncClient,
        auth_headers: dict,
    ) -> None:
        count = await create_health_action_notifications(
            session=db_session,
            tenant_id=sample_tenant.tenant_id,
            title="Revisá tu caja",
            action_text="Tu cobertura de caja está baja.",
            insight_id=uuid.uuid4(),
            action_suggestion_id=uuid.uuid4(),
            risk_code="CASH_LOW",
            now=datetime.now(UTC),
        )
        await db_session.commit()

        assert count == 1
        result = await db_session.execute(select(Notification))
        notification = result.scalar_one()
        assert notification.user_id == sample_user.user_id
        assert notification.notification_type == "health_action"
        assert notification.metadata_["action_url"] == "/dashboard?focus=health"

        resp = await client.get("/api/v1/notifications", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["notifications"][0]["action_url"] == "/dashboard?focus=health"


@pytest.mark.asyncio
class TestUnreadCountDecreasesAfterRead:
    async def test_unread_count_decreases_after_read(
        self,
        db_session: AsyncSession,
        sample_tenant: Tenant,
        sample_user: User,
        client: AsyncClient,
        auth_headers: dict,
    ) -> None:
        n1 = await _create_notification(db_session, sample_tenant, sample_user, title="Notif 1")
        n2 = await _create_notification(db_session, sample_tenant, sample_user, title="Notif 2")

        # Verify initial unread count
        resp = await client.get("/api/v1/notifications", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["unread_count"] == 2

        # Mark first notification as read
        resp = await client.patch(
            f"/api/v1/notifications/{n1.id}/read", headers=auth_headers
        )
        assert resp.status_code == 200

        # Unread count should decrease to 1
        resp = await client.get("/api/v1/notifications", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["unread_count"] == 1

        # Mark second as read
        resp = await client.patch(
            f"/api/v1/notifications/{n2.id}/read", headers=auth_headers
        )
        assert resp.status_code == 200

        resp = await client.get("/api/v1/notifications", headers=auth_headers)
        assert resp.json()["unread_count"] == 0

    async def test_mark_nonexistent_notification_returns_404(
        self,
        client: AsyncClient,
        auth_headers: dict,
    ) -> None:
        fake_id = uuid.uuid4()
        resp = await client.patch(
            f"/api/v1/notifications/{fake_id}/read", headers=auth_headers
        )
        assert resp.status_code == 404

    async def test_tenant_isolation(
        self,
        db_session: AsyncSession,
        sample_tenant: Tenant,
        sample_user: User,
        client: AsyncClient,
        auth_headers: dict,
        second_auth_headers: dict,
    ) -> None:
        """Notifications from tenant A must not be visible to tenant B."""
        await _create_notification(db_session, sample_tenant, sample_user)

        resp = await client.get("/api/v1/notifications", headers=second_auth_headers)
        assert resp.status_code == 200
        assert resp.json()["unread_count"] == 0
        assert resp.json()["notifications"] == []
