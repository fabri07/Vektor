"""Pydantic schemas for business profile endpoints."""

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, Field


class BusinessProfileResponse(BaseModel):
    model_config = {"from_attributes": True}

    id: UUID
    tenant_id: UUID
    legal_name: str
    trade_name: str
    cuit: str
    vertical: str
    size: str
    province: str
    city: str
    employee_count: int
    monthly_rent: Decimal | None
    created_at: datetime
    updated_at: datetime


class CreateBusinessProfileRequest(BaseModel):
    legal_name: str = Field(min_length=2, max_length=300)
    trade_name: str = Field(min_length=2, max_length=300)
    cuit: str = Field(pattern=r"^\d{2}-\d{8}-\d{1}$")  # XX-XXXXXXXX-X
    vertical: str = Field(pattern=r"^(kiosco|decoracion_hogar|limpieza)$")
    size: str = Field(pattern=r"^(micro|small|medium)$", default="micro")
    province: str = Field(min_length=2, max_length=100)
    city: str = Field(min_length=2, max_length=100)
    employee_count: int = Field(ge=1, le=10000)
    monthly_rent: Decimal | None = Field(default=None, ge=0)


class UpdateBusinessProfileRequest(BaseModel):
    legal_name: str | None = Field(default=None, min_length=2, max_length=300)
    trade_name: str | None = Field(default=None, min_length=2, max_length=300)
    size: str | None = Field(default=None, pattern=r"^(micro|small|medium)$")
    province: str | None = Field(default=None, min_length=2, max_length=100)
    city: str | None = Field(default=None, min_length=2, max_length=100)
    employee_count: int | None = Field(default=None, ge=1, le=10000)
    monthly_rent: Decimal | None = Field(default=None, ge=0)
