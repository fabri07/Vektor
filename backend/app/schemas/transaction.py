"""Pydantic schemas for sales and expense endpoints."""

import math
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_serializer, field_validator, model_validator

# Maximum amount accepted for a single transaction (999,999,999 ARS)
_MAX_AMOUNT = Decimal("999999999")


def _reject_nan_inf(v: Decimal | None, field_name: str = "amount") -> Decimal | None:
    """Rechaza NaN/Infinity de Decimals que provengan de floats."""
    if v is None:
        return v
    try:
        fv = float(v)
        if math.isnan(fv) or math.isinf(fv):
            raise ValueError(f"{field_name} no puede ser NaN ni Infinity.")
    except (ValueError, OverflowError) as exc:
        raise ValueError(str(exc)) from exc
    return v


# ── Sales ─────────────────────────────────────────────────────────────────────


class SaleEntryResponse(BaseModel):
    model_config = {"from_attributes": True}

    id: UUID
    tenant_id: UUID
    product_id: UUID | None
    amount: Decimal
    quantity: int
    transaction_date: datetime
    payment_method: str
    notes: str | None
    custom_fields: dict[str, Any] = {}
    created_at: datetime

    @field_serializer("amount")
    def _serialize_amount(self, v: Decimal) -> float:
        # El frontend tipa `amount` como `number` y hace aritmética (reduce sum).
        # Serializar Decimal como número evita la concatenación de strings que
        # producía `$ NaN` en totales con montos decimales.
        return float(v)


class CreateSaleRequest(BaseModel):
    amount: Decimal = Field(gt=0, le=_MAX_AMOUNT, decimal_places=2)
    quantity: int = Field(ge=1, default=1)
    transaction_date: datetime
    payment_method: str = Field(
        pattern=r"^(cash|debit_card|credit_card|transfer|qr|other)$", default="cash"
    )
    product_id: UUID | None = None
    notes: str | None = Field(default=None, max_length=1000)
    custom_fields: dict[str, Any] = Field(default_factory=dict)

    @field_validator("amount")
    @classmethod
    def amount_no_nan(cls, v: Decimal) -> Decimal:
        return _reject_nan_inf(v, "amount")  # type: ignore[return-value]

    @field_validator("transaction_date")
    @classmethod
    def transaction_date_not_future(cls, v: datetime) -> datetime:
        # Compara solo la fecha: se permite registrar hoy a cualquier hora.
        if v.date() > date.today():
            raise ValueError("transaction_date cannot be in the future.")
        return v


class UpdateSaleRequest(BaseModel):
    amount: Decimal | None = Field(default=None, gt=0, le=_MAX_AMOUNT, decimal_places=2)
    quantity: int | None = Field(default=None, ge=1)
    transaction_date: datetime | None = None
    payment_method: str | None = Field(
        default=None,
        pattern=r"^(cash|debit_card|credit_card|transfer|qr|other)$",
    )
    product_id: UUID | None = None
    notes: str | None = Field(default=None, max_length=1000)
    custom_fields: dict[str, Any] | None = None

    @field_validator("transaction_date")
    @classmethod
    def transaction_date_not_future(cls, v: datetime | None) -> datetime | None:
        if v is not None and v.date() > date.today():
            raise ValueError("transaction_date cannot be in the future.")
        return v


class BulkSaleEntryItem(BaseModel):
    product_id: UUID | None = None
    quantity: int = Field(ge=1, default=1)
    amount_ars: Decimal = Field(gt=0, le=_MAX_AMOUNT, decimal_places=2)


class BulkSaleRequest(BaseModel):
    period_type: Literal["daily", "weekly", "monthly"]
    period_date: date
    total_amount_ars: Decimal = Field(gt=0, le=_MAX_AMOUNT, decimal_places=2)
    entries: list[BulkSaleEntryItem] | None = None

    @field_validator("period_date")
    @classmethod
    def period_date_not_future(cls, v: date) -> date:
        if v > date.today():
            raise ValueError("period_date cannot be in the future.")
        return v


class SaleSummaryResponse(BaseModel):
    total_ars: float
    entry_count: int
    period_covered: str


class DateRangeResponse(BaseModel):
    """Rango de fechas con datos del tenant. None cuando no hay registros."""

    min_date: date | None
    max_date: date | None


# ── Expenses ──────────────────────────────────────────────────────────────────

EXPENSE_CATEGORIES = r"^(RENT|UTILITIES|PAYROLL|INVENTORY|MARKETING|OTHER)$"


class ExpenseEntryResponse(BaseModel):
    model_config = {"from_attributes": True}

    id: UUID
    tenant_id: UUID
    amount: Decimal
    category: str
    # Etiqueta libre cuando category == OTHER (guardada en custom_fields["category_label"]).
    # La categoría canónica sigue siendo OTHER → reportes/agregaciones no se rompen.
    category_label: str | None = None
    transaction_date: datetime
    description: str
    is_recurring: bool
    payment_method: str
    supplier_name: str | None
    notes: str | None
    custom_fields: dict[str, Any] = {}
    created_at: datetime

    @field_serializer("amount")
    def _serialize_amount(self, v: Decimal) -> float:
        # Ver nota en SaleEntryResponse._serialize_amount: número, no string,
        # para que el frontend pueda sumar sin producir NaN.
        return float(v)

    @model_validator(mode="after")
    def _extract_category_label(self) -> "ExpenseEntryResponse":
        if self.category_label is None and isinstance(self.custom_fields, dict):
            label = self.custom_fields.get("category_label")
            if isinstance(label, str) and label.strip():
                self.category_label = label.strip()
        return self


class CreateExpenseRequest(BaseModel):
    amount: Decimal = Field(gt=0, le=_MAX_AMOUNT, decimal_places=2)
    category: str = Field(pattern=EXPENSE_CATEGORIES)
    # Solo se usa cuando category == OTHER: nombre personalizado de la categoría.
    category_label: str | None = Field(default=None, max_length=50)
    expense_date: datetime
    notes: str | None = Field(default=None, max_length=1000)
    description: str = Field(default="", max_length=500)
    is_recurring: bool = False
    payment_method: str = Field(
        pattern=r"^(cash|debit_card|credit_card|transfer|qr|other)$",
        default="transfer",
    )
    supplier_name: str | None = Field(default=None, max_length=300)
    custom_fields: dict[str, Any] = Field(default_factory=dict)

    @field_validator("amount")
    @classmethod
    def amount_no_nan(cls, v: Decimal) -> Decimal:
        return _reject_nan_inf(v, "amount")  # type: ignore[return-value]

    @field_validator("expense_date")
    @classmethod
    def expense_date_not_future(cls, v: datetime) -> datetime:
        if v.date() > date.today():
            raise ValueError("expense_date cannot be in the future.")
        return v


class UpdateExpenseRequest(BaseModel):
    amount: Decimal | None = Field(default=None, gt=0, le=_MAX_AMOUNT, decimal_places=2)
    category: str | None = Field(default=None, pattern=EXPENSE_CATEGORIES)
    category_label: str | None = Field(default=None, max_length=50)
    expense_date: datetime | None = None
    description: str | None = Field(default=None, max_length=500)
    is_recurring: bool | None = None
    supplier_name: str | None = Field(default=None, max_length=300)
    notes: str | None = Field(default=None, max_length=1000)
    custom_fields: dict[str, Any] | None = None

    @field_validator("expense_date")
    @classmethod
    def expense_date_not_future(cls, v: datetime | None) -> datetime | None:
        if v is not None and v.date() > date.today():
            raise ValueError("expense_date cannot be in the future.")
        return v


class ExpenseSummaryResponse(BaseModel):
    total_ars: float
    entry_count: int
    period_covered: str
