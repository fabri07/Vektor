"""Onboarding service: processes initial business data for a tenant."""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.work_schedule_service import (
    DEFAULT_CLOSE_HOUR,
    DEFAULT_OPEN_HOUR,
    DEFAULT_WORK_DAYS,
)
from app.domain.verticals import heuristic_profile_version, parse_vertical
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
    """Cuánto sabemos del negocio, medido por lo que el dueño CONTESTÓ.

    Los tres montos puntúan por presencia (``is not None``), no por ser mayores
    a cero. Un negocio que contesta "cero gastos fijos" dio un dato tan bueno
    como el que contesta cien mil; el que dejó el campo en blanco no dio
    ninguno. Con ``> 0`` los dos casos valían lo mismo, y con los 20 puntos de
    caja incondicionales un campo que nunca se tipeó contaba como dato presente.
    """
    score = 25  # ventas: siempre > 0 (lo valida el schema)
    if body.monthly_inventory_cost_ars is not None:
        score += 20
    if body.monthly_fixed_expenses_ars is not None:
        score += 15
    if body.cash_on_hand_ars is not None:
        score += 20
    if body.product_count_estimate >= 5:
        score += 10
    if body.supplier_count_estimate >= 1:
        score += 10
    return score


def _monto_crudo(valor: Decimal | None) -> str | None:
    """Serializa un monto para `raw_inputs_json`, preservando la ausencia."""
    return None if valor is None else str(valor)


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

        # Step 4: persist financial estimates to business_profile. El vertical
        # NO se toca acá: ya lo fijó el dueño al aprobar la solicitud de
        # acceso y el usuario no puede reescribirlo (`vertical_code` salió del
        # request). Se lee del profile y se parsea estricto — un dato
        # corrupto/legado tiene que fallar ruidoso, no autodeterminarse.
        vertical = parse_vertical(bp.vertical_code)
        bp.monthly_sales_estimate_ars = monthly_sales
        bp.monthly_inventory_spend_estimate_ars = body.monthly_inventory_cost_ars
        bp.monthly_fixed_expenses_estimate_ars = body.monthly_fixed_expenses_ars
        bp.cash_on_hand_estimate_ars = body.cash_on_hand_ars
        bp.product_count_estimate = body.product_count_estimate
        bp.supplier_count_estimate = body.supplier_count_estimate
        bp.heuristic_profile_version = heuristic_profile_version(vertical)
        bp.heuristics_version = "v1"

        # Días y horarios laborales (Sprint 20): persistir lo enviado o defaults
        # Lun-Sáb 09-18, para dejar la cuenta configurada desde el alta.
        bp.work_days = body.work_days if body.work_days is not None else DEFAULT_WORK_DAYS
        bp.work_open_hour = (
            body.work_open_hour if body.work_open_hour is not None else DEFAULT_OPEN_HOUR
        )
        bp.work_close_hour = (
            body.work_close_hour if body.work_close_hour is not None else DEFAULT_CLOSE_HOUR
        )

        # Condición fiscal (opcional, informativa): solo se persiste si vino.
        # Si falta, no se toca (queda NULL = no configurado). No bloquea el alta.
        if body.fiscal_condition is not None:
            bp.fiscal_condition = body.fiscal_condition
        await self._repo.save(bp)

        # Step 5-6: calculate completeness and confidence
        completeness = _calculate_completeness(body)
        confidence = _derive_confidence(completeness)

        # Step 6b: resolver main_concern. Ahora se pregunta en el formulario
        # público de solicitud de acceso; si no vino en este body, se busca en
        # custom_fields (la aprobación lo copió ahí). Si tampoco está, se omite
        # del snapshot — no se inventa un valor.
        main_concern = body.main_concern
        if main_concern is None:
            main_concern = bp.custom_fields.get("main_concern")

        # Step 7: create business snapshot
        now = datetime.now(UTC)
        # Un monto ausente viaja como `None`, no como el string "None": el
        # snapshot es la materia prima del score y de cualquier auditoría
        # posterior, así que tiene que distinguir "no contestó" de "contestó 0".
        raw_inputs: dict[str, Any] = {
            "weekly_sales_estimate_ars": str(body.weekly_sales_estimate_ars),
            "monthly_inventory_cost_ars": _monto_crudo(body.monthly_inventory_cost_ars),
            "monthly_fixed_expenses_ars": _monto_crudo(body.monthly_fixed_expenses_ars),
            "cash_on_hand_ars": _monto_crudo(body.cash_on_hand_ars),
            "product_count_estimate": body.product_count_estimate,
            "supplier_count_estimate": body.supplier_count_estimate,
            "vertical_code": bp.vertical_code,
        }
        if main_concern is not None:
            raw_inputs["main_concern"] = main_concern
        snapshot = BusinessSnapshot(
            tenant_id=tenant_id,
            snapshot_date=now,
            snapshot_version="onboarding_v1",
            raw_inputs_json=raw_inputs,
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
