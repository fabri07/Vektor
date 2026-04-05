"""Pydantic schemas for sales and expense endpoints."""

from datetime import date, datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

# Maximum amount accepted for a single transaction (999,999,999 ARS)
_MAX_AMOUNT = Decimal("999999999")


# ── Sales ─────────────────────────────────────────────────────────────────────

class SaleEntryResponse(BaseModel):
    model_config = {"from_attributes": True}

    id: UUID
    tenant_id: UUID
    product_id: UUID | None
    amount: float
    quantity: int
    transaction_date: date
    payment_method: str
    notes: str | None
    created_at: datetime


class CreateSaleRequest(BaseModel):
    amount: Decimal = Field(gt=0, le=_MAX_AMOUNT, decimal_places=2)
    quantity: int = Field(ge=1, default=1)
    transaction_date: date
    payment_method: str = Field(
        pattern=r"^(cash|debit_card|credit_card|transfer|qr|other)$", default="cash"
    )
    product_id: UUID | None = None
    notes: str | None = Field(default=None, max_length=1000)

    @field_validator("transaction_date")
    @classmethod
    def transaction_date_not_future(cls, v: date) -> date:
        if v > date.today():
            raise ValueError("transaction_date cannot be in the future.")
        return v


class UpdateSaleRequest(BaseModel):
    amount: Decimal | None = Field(default=None, gt=0, le=_MAX_AMOUNT, decimal_places=2)
    quantity: int | None = Field(default=None, ge=1)
    transaction_date: date | None = None
    payment_method: str | None = Field(
        default=None,
        pattern=r"^(cash|debit_card|credit_card|transfer|qr|other)$",
    )
    notes: str | None = Field(default=None, max_length=1000)

    @field_validator("transaction_date")
    @classmethod
    def transaction_date_not_future(cls, v: date | None) -> date | None:
        if v is not None and v > date.today():
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


# ── Expenses ──────────────────────────────────────────────────────────────────

EXPENSE_CATEGORIES = r"^(RENT|UTILITIES|PAYROLL|INVENTORY|MARKETING|OTHER)$"


class ExpenseEntryResponse(BaseModel):
    model_config = {"from_attributes": True}

    id: UUID
    tenant_id: UUID
    amount: float
    category: str
    transaction_date: date
    description: str
    is_recurring: bool
    payment_method: str
    supplier_name: str | None
    notes: str | None
    created_at: datetime


class CreateExpenseRequest(BaseModel):
    amount: Decimal = Field(gt=0, le=_MAX_AMOUNT, decimal_places=2)
    category: str = Field(pattern=EXPENSE_CATEGORIES)
    expense_date: date
    notes: str | None = Field(default=None, max_length=1000)
    description: str = Field(default="", max_length=500)
    is_recurring: bool = False
    payment_method: str = Field(
        pattern=r"^(cash|debit_card|credit_card|transfer|qr|other)$",
        default="transfer",
    )
    supplier_name: str | None = Field(default=None, max_length=300)

    @field_validator("expense_date")
    @classmethod
    def expense_date_not_future(cls, v: date) -> date:
        if v > date.today():
            raise ValueError("expense_date cannot be in the future.")
        return v


class UpdateExpenseRequest(BaseModel):
    amount: Decimal | None = Field(default=None, gt=0, le=_MAX_AMOUNT, decimal_places=2)
    category: str | None = Field(default=None, pattern=EXPENSE_CATEGORIES)
    expense_date: date | None = None
    description: str | None = Field(default=None, max_length=500)
    is_recurring: bool | None = None
    supplier_name: str | None = Field(default=None, max_length=300)
    notes: str | None = Field(default=None, max_length=1000)

    @field_validator("expense_date")
    @classmethod
    def expense_date_not_future(cls, v: date | None) -> date | None:
        if v is not None and v > date.today():
            raise ValueError("expense_date cannot be in the future.")
        return v


class ExpenseSummaryResponse(BaseModel):
    total_ars: float
    entry_count: int
    period_covered: str
