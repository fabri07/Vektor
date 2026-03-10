"""Pydantic schemas for user endpoints."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field


class UserResponse(BaseModel):
    model_config = {"from_attributes": True}

    id: UUID
    tenant_id: UUID
    email: str
    full_name: str
    role: str
    status: str
    created_at: datetime
    last_login_at: datetime | None


class CreateUserRequest(BaseModel):
    email: EmailStr
    full_name: str = Field(min_length=2, max_length=200)
    role: str = Field(pattern=r"^(owner|admin|analyst|viewer)$")
    password: str = Field(min_length=8, max_length=128)


class UpdateUserRequest(BaseModel):
    full_name: str | None = Field(default=None, min_length=2, max_length=200)
    role: str | None = Field(
        default=None, pattern=r"^(owner|admin|analyst|viewer)$"
    )
