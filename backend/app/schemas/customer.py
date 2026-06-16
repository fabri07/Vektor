"""Pydantic schemas for customer endpoints."""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field, computed_field


class CustomerResponse(BaseModel):
    model_config = {"from_attributes": True}

    id: UUID
    tenant_id: UUID
    name: str
    email: str | None
    phone: str | None
    telegram_username: str | None
    notes: str | None
    custom_fields: dict[str, Any] = {}
    created_at: datetime
    deactivated_at: datetime | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_active(self) -> bool:
        return self.deactivated_at is None


class CreateCustomerRequest(BaseModel):
    name: str = Field(min_length=1, max_length=300)
    email: str | None = Field(default=None, max_length=320)
    phone: str | None = Field(default=None, max_length=50)
    telegram_username: str | None = Field(default=None, max_length=100)
    notes: str | None = Field(default=None, max_length=2000)
    custom_fields: dict[str, Any] = Field(default_factory=dict)


class UpdateCustomerRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=300)
    email: str | None = Field(default=None, max_length=320)
    phone: str | None = Field(default=None, max_length=50)
    telegram_username: str | None = Field(default=None, max_length=100)
    notes: str | None = Field(default=None, max_length=2000)
    custom_fields: dict[str, Any] | None = None
