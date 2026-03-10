"""Pydantic schemas for tenant endpoints."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class TenantResponse(BaseModel):
    model_config = {"from_attributes": True}

    id: UUID
    name: str
    slug: str
    vertical: str
    status: str
    created_at: datetime


class TenantUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=200)
