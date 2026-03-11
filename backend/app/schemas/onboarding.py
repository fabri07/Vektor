"""Pydantic schemas for onboarding endpoints."""

from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, Field


class OnboardingSubmitRequest(BaseModel):
    vertical_code: str = Field(pattern=r"^(kiosco|decoracion_hogar|limpieza)$")
    weekly_sales_estimate_ars: Decimal = Field(gt=0)
    monthly_inventory_cost_ars: Decimal = Field(ge=0)
    monthly_fixed_expenses_ars: Decimal = Field(ge=0)
    cash_on_hand_ars: Decimal = Field(ge=0)
    product_count_estimate: int = Field(ge=0)
    supplier_count_estimate: int = Field(ge=0)
    main_concern: str = Field(pattern=r"^(MARGIN|STOCK|CASH)$")


class OnboardingSubmitResponse(BaseModel):
    snapshot_id: UUID
    data_completeness_score: int
    confidence_level: str
    message: str


class OnboardingStatusResponse(BaseModel):
    completed: bool
    vertical_code: str
    data_completeness_score: int | None
