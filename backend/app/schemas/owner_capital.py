"""Schemas de capital de los socios (D·2)."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_serializer, field_validator

from app.domain.business_time import today_ar
from app.schemas.transaction import _MAX_AMOUNT, PAYMENT_METHOD_PATTERN, _reject_nan_inf

CapitalKind = Literal["aporte", "retiro", "prestamo_socio", "devolucion_prestamo"]


class CreateCapitalMovementRequest(BaseModel):
    kind: CapitalKind
    # Sólo dinero en este paso: el aporte en especie (mercadería atada al stock,
    # equipamiento informativo) es D·6 y tiene reglas propias.
    form: Literal["dinero"] = "dinero"
    amount: Decimal = Field(gt=0, le=_MAX_AMOUNT, decimal_places=2)
    occurred_on: date
    payment_method: str = Field(pattern=PAYMENT_METHOD_PATTERN, default="cash")
    partner_name: str | None = Field(default=None, max_length=120)
    notes: str | None = Field(default=None, max_length=1000)

    @field_validator("amount")
    @classmethod
    def amount_no_nan(cls, v: Decimal) -> Decimal:
        return _reject_nan_inf(v, "amount")  # type: ignore[return-value]

    @field_validator("occurred_on")
    @classmethod
    def occurred_on_not_future(cls, v: date) -> date:
        if v > today_ar():
            raise ValueError("occurred_on cannot be in the future.")
        return v

    @field_validator("partner_name", "notes")
    @classmethod
    def blank_to_none(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        return v or None


class VoidCapitalMovementRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=500)

    @field_validator("reason")
    @classmethod
    def reason_not_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("reason is required.")
        return v


class CapitalMovementResponse(BaseModel):
    model_config = {"from_attributes": True}

    id: UUID
    kind: str
    form: str
    amount_ars: Decimal
    occurred_on: date
    payment_method: str | None
    partner_name: str | None
    notes: str | None
    source_type: str
    created_at: datetime
    created_by_user_id: UUID | None
    voided_at: datetime | None
    void_reason: str | None

    @field_serializer("amount_ars")
    def _serialize_amount(self, v: Decimal) -> float:
        return float(v)


class CapitalMovementListResponse(BaseModel):
    items: list[CapitalMovementResponse]
