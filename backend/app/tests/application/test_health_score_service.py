"""Integration tests for HealthScoreService."""


from datetime import UTC, datetime
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.health_score_service import HealthScoreService
from app.persistence.models.business import BusinessProfile
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import SaleEntry
from app.persistence.repositories.health_score_repository import HealthScoreRepository


@pytest_asyncio.fixture
async def kiosco_profile(db_session: AsyncSession, sample_tenant: Tenant) -> BusinessProfile:
    profile = BusinessProfile(
        tenant_id=sample_tenant.tenant_id,
        vertical_code="kiosco",
        data_mode="M0",
        data_confidence="LOW",
        monthly_sales_estimate_ars=Decimal("150000.00"),
        monthly_inventory_spend_estimate_ars=Decimal("90000.00"),
        monthly_fixed_expenses_estimate_ars=Decimal("20000.00"),
        cash_on_hand_estimate_ars=Decimal("15000.00"),
        supplier_count_estimate=2,
        product_count_estimate=3,
        onboarding_completed=True,
        updated_at=datetime.now(UTC),
        created_at=datetime.now(UTC),
    )
    db_session.add(profile)
    await db_session.commit()
    return profile


@pytest.mark.asyncio
class TestHealthScoreService:
    async def test_recalculate_no_data_produces_low_score(
        self,
        db_session: AsyncSession,
        sample_tenant: Tenant,
        kiosco_profile: BusinessProfile,
    ) -> None:
        """With no sales or expenses, the score should be low."""
        svc = HealthScoreService(db_session)
        snapshot = await svc.recalculate_for_tenant(
            tenant_id=sample_tenant.tenant_id,
            triggered_by="test",
        )
        await db_session.commit()
        assert snapshot.tenant_id == sample_tenant.tenant_id
        assert float(snapshot.total_score) >= 0
        assert snapshot.level in ("critical", "warning", "healthy", "excellent")

    async def test_recalculate_stores_snapshot(
        self,
        db_session: AsyncSession,
        sample_tenant: Tenant,
        kiosco_profile: BusinessProfile,
    ) -> None:
        svc = HealthScoreService(db_session)
        await svc.recalculate_for_tenant(
            tenant_id=sample_tenant.tenant_id,
            triggered_by="test",
        )
        await db_session.commit()

        repo = HealthScoreRepository(db_session)
        latest = await repo.get_latest(sample_tenant.tenant_id)
        assert latest is not None
        assert latest.triggered_by == "test"

    async def test_recalculate_with_sales_improves_score(
        self,
        db_session: AsyncSession,
        sample_tenant: Tenant,
        kiosco_profile: BusinessProfile,
    ) -> None:
        from datetime import date  # noqa: PLC0415

        # Add some sales
        for _i in range(10):
            sale = SaleEntry(
                tenant_id=sample_tenant.tenant_id,
                amount=Decimal("50000"),
                quantity=1,
                transaction_date=date.today(),
                payment_method="cash",
            )
            db_session.add(sale)
        await db_session.flush()

        svc = HealthScoreService(db_session)
        snapshot_with_sales = await svc.recalculate_for_tenant(
            tenant_id=sample_tenant.tenant_id,
            triggered_by="test_with_sales",
        )
        await db_session.commit()
        assert float(snapshot_with_sales.total_score) > 0
