"""Pydantic schemas for field definitions endpoints."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class EnumOption(BaseModel):
    value: str
    label: str


class FieldDefinitionResponse(BaseModel):
    field_key: str
    entity_type: str
    label: str
    data_type: str
    enum_options: list[EnumOption] | None = None
    is_required: bool
    display_order: int
    is_base_field: bool
    affects_scoring: bool


class CreateCustomFieldRequest(BaseModel):
    entity_type: str = Field(
        pattern="^(sale|expense|product|inventory|customer|supplier|marketing)$"
    )
    field_key: str = Field(min_length=2, max_length=80, pattern="^[a-z][a-z0-9_]*$")
    label: str = Field(min_length=1, max_length=200)
    data_type: str = Field(default="text", pattern="^(text|number|date|enum|boolean)$")
    enum_options: list[EnumOption] | None = None
    display_order: int = Field(default=0, ge=0)


class UpdateCustomFieldRequest(BaseModel):
    label: str | None = Field(default=None, min_length=1, max_length=200)
    override_required: bool | None = None
    override_enum_options: list[EnumOption] | None = None
    display_order: int | None = Field(default=None, ge=0)


class ToggleFieldRequest(BaseModel):
    enabled: bool


class FieldChangeLogResponse(BaseModel):
    id: uuid.UUID
    field_key: str
    entity_type: str
    action: str
    previous_state: dict[str, Any] | None
    new_state: dict[str, Any]
    changed_at: datetime

    model_config = {"from_attributes": True}
