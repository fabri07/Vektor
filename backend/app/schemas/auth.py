"""Pydantic schemas for authentication endpoints."""

from uuid import UUID

from pydantic import BaseModel, EmailStr, Field, field_validator
from pydantic_core import PydanticCustomError


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8)


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    full_name: str = Field(min_length=2, max_length=200)
    business_name: str = Field(min_length=2, max_length=200)
    vertical_code: str = Field(pattern=r"^(kiosco|decoracion_hogar|limpieza)$")

    @field_validator("password")
    @classmethod
    def password_strength(cls, v: str) -> str:
        if not any(c.isdigit() for c in v):
            raise ValueError("Password must contain at least one digit.")
        if not any(c.isalpha() for c in v):
            raise ValueError("Password must contain at least one letter.")
        return v


def validate_password_strength(value: str) -> str:
    if not any(c.isdigit() for c in value):
        raise PydanticCustomError(
            "password_strength",
            "Password must contain at least one digit.",
        )
    if not any(c.isalpha() for c in value):
        raise PydanticCustomError(
            "password_strength",
            "Password must contain at least one letter.",
        )
    return value


class UserInAuthResponse(BaseModel):
    """Embedded user data returned on register/login."""

    user_id: UUID
    email: str
    full_name: str
    role_code: str
    tenant_id: UUID


class AuthResponse(BaseModel):
    """Response for POST /register and POST /login."""

    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int  # seconds
    user: UserInAuthResponse


class SubscriptionInMeResponse(BaseModel):
    plan_code: str
    status: str


class MeResponse(BaseModel):
    """Response for GET /auth/me."""

    user_id: UUID
    email: str
    full_name: str
    role_code: str
    tenant_id: UUID
    subscription: SubscriptionInMeResponse | None
    onboarding_completed: bool


class TokenResponse(BaseModel):
    """Used by POST /refresh only."""

    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int  # seconds


class RefreshRequest(BaseModel):
    refresh_token: str


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str = Field(min_length=8, max_length=128)

    @field_validator("new_password")
    @classmethod
    def password_strength(cls, v: str) -> str:
        return validate_password_strength(v)


_PIN_PATTERN = r"^\d{4}$"


class PinSetupRequest(BaseModel):
    pin: str = Field(pattern=_PIN_PATTERN)
    pin_confirm: str = Field(pattern=_PIN_PATTERN)


class PinVerifyRequest(BaseModel):
    pin: str = Field(pattern=_PIN_PATTERN)


class PinChangeRequest(BaseModel):
    current_pin: str = Field(pattern=_PIN_PATTERN)
    new_pin: str = Field(pattern=_PIN_PATTERN)
    new_pin_confirm: str = Field(pattern=_PIN_PATTERN)


class PinResetRequest(BaseModel):
    password: str = Field(min_length=8, max_length=128)
    new_pin: str = Field(pattern=_PIN_PATTERN)
    new_pin_confirm: str = Field(pattern=_PIN_PATTERN)


class PinStatusResponse(BaseModel):
    pin_set: bool
    verified: bool
    must_set: bool


class RegisterResponse(BaseModel):
    """Response for POST /register.
    requires_verification=True means the user must click the email link before logging in.
    requires_verification=False means the account is active immediately (e.g. DEBUG mode).
    """

    message: str
    email: str
    requires_verification: bool = True


class VerifyEmailRequest(BaseModel):
    token: str


class ResendVerificationRequest(BaseModel):
    email: EmailStr


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    token: str
    new_password: str = Field(min_length=8, max_length=128)

    @field_validator("new_password")
    @classmethod
    def password_strength(cls, v: str) -> str:
        return validate_password_strength(v)
