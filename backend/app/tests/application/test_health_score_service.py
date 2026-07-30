"""Integration tests for HealthScoreService."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.health_score_service import HealthScoreService
from app.persistence.models.business import BusinessProfile
from app.persistence.models.heuristic_override import BusinessHeuristicOverride
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import SaleEntry
from app.persistence.repositories.health_score_repository import HealthScoreRepository


@pytest_asyncio.fixture
async def kiosco_profile(
    db_session: AsyncSession, sample_business_profile: BusinessProfile
) -> BusinessProfile:
    profile = sample_business_profile
    profile.monthly_sales_estimate_ars = Decimal("150000.00")
    profile.monthly_inventory_spend_estimate_ars = Decimal("90000.00")
    profile.monthly_fixed_expenses_estimate_ars = Decimal("20000.00")
    profile.cash_on_hand_estimate_ars = Decimal("15000.00")
    profile.supplier_count_estimate = 2
    profile.product_count_estimate = 3
    profile.onboarding_completed = True
    profile.updated_at = datetime.now(UTC)
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

    async def test_recalculate_con_override_de_margen_no_rompe(
        self,
        db_session: AsyncSession,
        sample_tenant: Tenant,
        kiosco_profile: BusinessProfile,
    ) -> None:
        """Un tenant que configuró su margen objetivo debe poder recalcular.

        Regresión: `analytics_svc` se instanciaba DENTRO del `if tenant_benchmark
        is None` y se usaba incondicionalmente después para registrar el evento,
        así que la rama con override moría con `UnboundLocalError`. El camino se
        dispara con cualquier `PATCH /settings/health-config`, y como ni
        `score_trigger_service` ni `score_worker` lo atrapan, se llevaba puesto el
        recálculo del tenant (y el rebuild semanal de los que venían después).
        """
        for key, value in (("target_margin_pct", 35.0), ("warning_margin_pct", 20.0)):
            db_session.add(
                BusinessHeuristicOverride(
                    tenant_id=sample_tenant.tenant_id,
                    param_key=key,
                    param_value={"value": value},
                )
            )
        await db_session.flush()

        svc = HealthScoreService(db_session)
        snapshot = await svc.recalculate_for_tenant(
            tenant_id=sample_tenant.tenant_id,
            triggered_by="test_override",
        )
        await db_session.commit()

        assert snapshot.tenant_id == sample_tenant.tenant_id
        assert snapshot.triggered_by == "test_override"
