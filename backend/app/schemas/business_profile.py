"""Pydantic schemas for business profile endpoints."""

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, Field


class BusinessProfileResponse(BaseModel):
    model_config = {"from_attributes": True}

    profile_id: UUID
    tenant_id: UUID
    vertical_code: str
    data_mode: str
    data_confidence: str
    monthly_sales_estimate_ars: float | None
    monthly_inventory_spend_estimate_ars: float | None
    monthly_fixed_expenses_estimate_ars: float | None
    cash_on_hand_estimate_ars: float | None
    supplier_count_estimate: int | None
    product_count_estimate: int | None
    onboarding_completed: bool
    heuristic_profile_version: str
    created_at: datetime
    updated_at: datetime


class UpdateBusinessProfileRequest(BaseModel):
    """Onboarding and subsequent updates to the business profile."""

    monthly_sales_estimate_ars: Decimal | None = Field(default=None, ge=0)
    monthly_inventory_spend_estimate_ars: Decimal | None = Field(default=None, ge=0)
    monthly_fixed_expenses_estimate_ars: Decimal | None = Field(default=None, ge=0)
    cash_on_hand_estimate_ars: Decimal | None = Field(default=None, ge=0)
    supplier_count_estimate: int | None = Field(default=None, ge=0)
    product_count_estimate: int | None = Field(default=None, ge=0)
    onboarding_completed: bool | None = None
