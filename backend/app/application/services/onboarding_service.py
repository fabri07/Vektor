"""Onboarding service: processes initial business data for a tenant."""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.observability.logger import get_logger
from app.persistence.models.business import BusinessSnapshot
from app.persistence.repositories.business_profile_repository import (
    BusinessProfileRepository,
)
from app.schemas.onboarding import (
    OnboardingStatusResponse,
    OnboardingSubmitRequest,
    OnboardingSubmitResponse,
)

logger = get_logger(__name__)


class AlreadyOnboardedError(Exception):
    """Raised when a tenant tries to submit onboarding more than once."""


def _calculate_completeness(body: OnboardingSubmitRequest) -> int:
    score = 25  # ventas: always > 0 (validated by schema)
    if body.monthly_inventory_cost_ars > 0:
        score += 20
    if body.monthly_fixed_expenses_ars > 0:
        score += 15
    score += 20  # caja: always counted (>= 0 validated, data presence = 20 pts)
    if body.product_count_estimate >= 5:
        score += 10
    if body.supplier_count_estimate >= 1:
        score += 10
    return score


def _derive_confidence(score: int) -> str:
    if score >= 80:
        return "HIGH"
    if score >= 50:
        return "MEDIUM"
    return "LOW"


def _enqueue_score_recalculation(tenant_id: UUID, snapshot_id: UUID) -> None:
    try:
        from app.jobs.score_worker import trigger_score_recalculation  # noqa: PLC0415

        trigger_score_recalculation.delay(str(tenant_id), str(snapshot_id))
    except Exception:
        logger.warning(
            "onboarding.score_enqueue_failed",
            tenant_id=str(tenant_id),
            snapshot_id=str(snapshot_id),
        )


class OnboardingService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self._repo = BusinessProfileRepository(session)

    async def submit(
        self, tenant_id: UUID, body: OnboardingSubmitRequest
    ) -> OnboardingSubmitResponse:
        bp = await self._repo.get_by_tenant_id(tenant_id)
        if bp is None:
            raise ValueError("Business profile not found for tenant.")

        # Step 1: validate onboarding not already completed
        if bp.onboarding_completed:
            raise AlreadyOnboardedError()

        # Step 2-3: calculate monthly sales estimate
        monthly_sales = body.weekly_sales_estimate_ars * Decimal("4.3")

        # Step 4: persist vertical and financial estimates to business_profile
        bp.vertical_code = body.vertical_code
        bp.monthly_sales_estimate_ars = monthly_sales
        bp.monthly_inventory_spend_estimate_ars = body.monthly_inventory_cost_ars
        bp.monthly_fixed_expenses_estimate_ars = body.monthly_fixed_expenses_ars
        bp.cash_on_hand_estimate_ars = body.cash_on_hand_ars
        bp.product_count_estimate = body.product_count_estimate
        bp.supplier_count_estimate = body.supplier_count_estimate
        await self._repo.save(bp)

        # Step 5-6: calculate completeness and confidence
        completeness = _calculate_completeness(body)
        confidence = _derive_confidence(completeness)

        # Step 7: create business snapshot
        now = datetime.now(UTC)
        snapshot = BusinessSnapshot(
            tenant_id=tenant_id,
            snapshot_date=now,
            snapshot_version="onboarding_v1",
            raw_inputs_json={
                "weekly_sales_estimate_ars": str(body.weekly_sales_estimate_ars),
                "monthly_inventory_cost_ars": str(body.monthly_inventory_cost_ars),
                "monthly_fixed_expenses_ars": str(body.monthly_fixed_expenses_ars),
                "cash_on_hand_ars": str(body.cash_on_hand_ars),
                "product_count_estimate": body.product_count_estimate,
                "supplier_count_estimate": body.supplier_count_estimate,
                "main_concern": body.main_concern,
                "vertical_code": body.vertical_code,
            },
            data_completeness_score=Decimal(completeness),
            data_mode="M0",
            confidence_level=confidence,
            created_at=now,
        )
        snapshot = await self._repo.create_snapshot(snapshot)

        # Step 8: mark onboarding complete and update confidence on profile
        bp.onboarding_completed = True
        bp.data_confidence = confidence
        await self._repo.save(bp)

        # Step 9: enqueue async score recalculation
        _enqueue_score_recalculation(tenant_id=tenant_id, snapshot_id=snapshot.id)

        logger.info(
            "onboarding.submitted",
            tenant_id=str(tenant_id),
            completeness=completeness,
            confidence=confidence,
        )

        return OnboardingSubmitResponse(
            snapshot_id=snapshot.id,
            data_completeness_score=completeness,
            confidence_level=confidence,
            message="Procesando tu score...",
        )

    async def get_status(self, tenant_id: UUID) -> OnboardingStatusResponse:
        bp = await self._repo.get_by_tenant_id(tenant_id)
        if bp is None:
            return OnboardingStatusResponse(
                completed=False,
                vertical_code="",
                data_completeness_score=None,
            )

        completeness: int | None = None
        if bp.onboarding_completed:
            snapshot = await self._repo.get_latest_snapshot(tenant_id)
            if snapshot and snapshot.data_completeness_score is not None:
                completeness = int(snapshot.data_completeness_score)

        return OnboardingStatusResponse(
            completed=bp.onboarding_completed,
            vertical_code=bp.vertical_code,
            data_completeness_score=completeness,
        )
