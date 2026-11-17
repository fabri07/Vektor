"""Schemas de pagos de deuda a proveedores (PR7)."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator

from app.domain.business_time import today_ar
from app.schemas.transaction import _MAX_AMOUNT, _reject_nan_inf

#: Pagar «a cuenta corriente» no saca plata de ningún lado: no es un medio de pago acá.
SupplierPaymentMethod = Literal["cash", "debit_card", "credit_card", "transfer", "qr", "other"]


class CreateSupplierPaymentRequest(BaseModel):
    amount: Decimal = Field(gt=0, le=_MAX_AMOUNT, decimal_places=2)
    occurred_on: date
    payment_method: SupplierPaymentMethod
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

    @field_validator("notes")
    @classmethod
    def blank_to_none(cls, v: str | None) -> str | None:
        if v is None:
            return None
        return v.strip() or None


class ResolveSupplierPaymentFields(CreateSupplierPaymentRequest):
    """El pago de una fila de «Otros»: igual que el alta, más A QUÉ proveedor."""

    supplier_id: UUID


class VoidSupplierPaymentRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=500)

    @field_validator("reason")
    @classmethod
    def reason_not_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("reason is required.")
        return v


class SupplierPaymentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    supplier_id: UUID
    amount_ars: Decimal
    occurred_on: date
    payment_method: str
    notes: str | None
    source_type: str
    voided_at: datetime | None
    void_reason: str | None

    @field_serializer("amount_ars")
    def _amount(self, v: Decimal) -> str:
        return str(v)


class SupplierPaymentListResponse(BaseModel):
    items: list[SupplierPaymentResponse]


class SupplierBalanceResponse(BaseModel):
    supplier_id: UUID
    comprado_a_cuenta_corriente: Decimal
    pagado: Decimal
    deuda: Decimal

    @field_serializer("comprado_a_cuenta_corriente", "pagado", "deuda")
    def _money(self, v: Decimal) -> str:
        return str(v)
