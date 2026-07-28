"""Pydantic schemas for onboarding endpoints."""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

from app.domain.fiscal_condition import FiscalCondition
from app.domain.verticals import VERTICAL_CODE_PATTERN


class OnboardingSubmitRequest(BaseModel):
    vertical_code: str = Field(pattern=VERTICAL_CODE_PATTERN)
    weekly_sales_estimate_ars: Decimal = Field(gt=0)
    monthly_inventory_cost_ars: Decimal = Field(ge=0)
    monthly_fixed_expenses_ars: Decimal = Field(ge=0)
    cash_on_hand_ars: Decimal = Field(ge=0)
    product_count_estimate: int = Field(ge=0)
    supplier_count_estimate: int = Field(ge=0)
    main_concern: str = Field(pattern=r"^(MARGIN|STOCK|CASH)$")
    # Días y horarios laborales (Sprint 20) — opcionales; si faltan, se usan defaults.
    # Regla "todo o nada": o se envían los 3 (válidos) o ninguno. Evita que entre
    # una configuración parcial/inválida al crear la cuenta (misma validación que
    # WorkScheduleRequest en settings).
    work_days: list[int] | None = Field(default=None)
    work_open_hour: int | None = Field(default=None, ge=0, le=23)
    work_close_hour: int | None = Field(default=None, ge=0, le=23)
    # Condición fiscal — opcional y solo informativa. Si viene, se guarda en el
    # profile; si falta, queda NULL (no configurado) y NO bloquea el onboarding.
    fiscal_condition: FiscalCondition | None = Field(default=None)

    @model_validator(mode="after")
    def _validate_work_schedule(self) -> OnboardingSubmitRequest:
        provided = [
            self.work_days is not None,
            self.work_open_hour is not None,
            self.work_close_hour is not None,
        ]
        if not any(provided):
            return self  # ninguno → se usan defaults en el service
        if not all(provided):
            raise ValueError(
                "Enviá work_days, work_open_hour y work_close_hour juntos, o ninguno."
            )
        assert self.work_days is not None  # narrowing para mypy
        if not self.work_days:
            raise ValueError("work_days debe tener al menos un día.")
        if any(d < 0 or d > 6 for d in self.work_days):
            raise ValueError("work_days deben estar en 0-6 (0=lunes … 6=domingo).")
        if len(set(self.work_days)) != len(self.work_days):
            raise ValueError("work_days no puede tener días repetidos.")
        if (
            self.work_close_hour is not None
            and self.work_open_hour is not None
            and self.work_close_hour <= self.work_open_hour
        ):
            raise ValueError("work_close_hour debe ser mayor que work_open_hour.")
        return self


class OnboardingSubmitResponse(BaseModel):
    snapshot_id: UUID
    data_completeness_score: int
    confidence_level: str
    message: str


class OnboardingStatusResponse(BaseModel):
    completed: bool
    vertical_code: str
    data_completeness_score: int | None
