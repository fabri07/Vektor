"""Pydantic schemas for user endpoints."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field, field_validator
from pydantic_core import PydanticCustomError

from app.schemas._ar_fiscal import normalize_phone


class UserResponse(BaseModel):
    model_config = {"from_attributes": True}

    user_id: UUID
    tenant_id: UUID
    email: str
    full_name: str
    role_code: str
    phone: str | None
    is_active: bool
    created_at: datetime
    last_login_at: datetime | None


class CreateUserRequest(BaseModel):
    email: EmailStr
    full_name: str = Field(min_length=2, max_length=200)
    role_code: str = Field(pattern=r"^(OWNER|ADMIN|ANALYST|VIEWER)$")
    password: str = Field(min_length=8, max_length=128)


class UpdateUserRequest(BaseModel):
    full_name: str | None = Field(default=None, min_length=2, max_length=200)
    role_code: str | None = Field(default=None, pattern=r"^(OWNER|ADMIN|ANALYST|VIEWER)$")


class UpdateMeRequest(BaseModel):
    """Perfil propio (PATCH /users/me). Deliberadamente SIN role_code/email:
    un usuario solo puede tocar su nombre y su teléfono de contacto.
    ``phone: null`` explícito borra el número (se distingue de "no enviado"
    con ``model_fields_set`` en el endpoint)."""

    full_name: str | None = Field(default=None, min_length=2, max_length=200)
    phone: str | None = Field(default=None, max_length=50)

    @field_validator("full_name")
    @classmethod
    def full_name_strip(cls, v: str | None) -> str | None:
        # min_length cuenta espacios: "  " pasaría y dejaría el nombre en blanco.
        if v is None:
            return None
        stripped = v.strip()
        if len(stripped) < 2:
            raise PydanticCustomError(
                "full_name_blank", "El nombre debe tener al menos 2 caracteres."
            )
        return stripped

    @field_validator("phone")
    @classmethod
    def phone_strip(cls, v: str | None) -> str | None:
        return normalize_phone(v)
