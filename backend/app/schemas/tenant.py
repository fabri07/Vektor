"""Pydantic schemas for tenant endpoints."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class TenantResponse(BaseModel):
    model_config = {"from_attributes": True}

    tenant_id: UUID
    legal_name: str
    display_name: str
    currency: str
    pricing_reference_mode: str
    status: str
    created_at: datetime
    updated_at: datetime


class TenantUpdateRequest(BaseModel):
    legal_name: str | None = Field(default=None, min_length=2, max_length=200)
    display_name: str | None = Field(default=None, min_length=2, max_length=200)
