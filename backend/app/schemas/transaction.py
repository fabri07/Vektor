"""Pydantic schemas for sales and expense endpoints."""

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, Field


# ── Sales ─────────────────────────────────────────────────────────────────────

class SaleEntryResponse(BaseModel):
    model_config = {"from_attributes": True}

    id: UUID
    tenant_id: UUID
    product_id: UUID | None
    amount: Decimal
    quantity: int
    transaction_date: date
    payment_method: str
    notes: str | None
    created_at: datetime


class CreateSaleRequest(BaseModel):
    amount: Decimal = Field(gt=0, decimal_places=2)
    quantity: int = Field(ge=1, default=1)
    transaction_date: date
    payment_method: str = Field(
        pattern=r"^(cash|debit_card|credit_card|transfer|qr|other)$", default="cash"
    )
    product_id: UUID | None = None
    notes: str | None = Field(default=None, max_length=1000)


class UpdateSaleRequest(BaseModel):
    amount: Decimal | None = Field(default=None, gt=0, decimal_places=2)
    quantity: int | None = Field(default=None, ge=1)
    transaction_date: date | None = None
    payment_method: str | None = Field(
        default=None,
        pattern=r"^(cash|debit_card|credit_card|transfer|qr|other)$",
    )
    notes: str | None = Field(default=None, max_length=1000)


# ── Expenses ──────────────────────────────────────────────────────────────────

class ExpenseEntryResponse(BaseModel):
    model_config = {"from_attributes": True}

    id: UUID
    tenant_id: UUID
    amount: Decimal
    category: str
    transaction_date: date
    description: str
    is_recurring: bool
    payment_method: str
    supplier_name: str | None
    notes: str | None
    created_at: datetime


class CreateExpenseRequest(BaseModel):
    amount: Decimal = Field(gt=0, decimal_places=2)
    category: str = Field(
        pattern=r"^(rent|utilities|salaries|suppliers|taxes|marketing|maintenance|financing|other)$"
    )
    transaction_date: date
    description: str = Field(min_length=2, max_length=500)
    is_recurring: bool = False
    payment_method: str = Field(
        pattern=r"^(cash|debit_card|credit_card|transfer|qr|other)$",
        default="transfer",
    )
    supplier_name: str | None = Field(default=None, max_length=300)
    notes: str | None = Field(default=None, max_length=1000)


class UpdateExpenseRequest(BaseModel):
    amount: Decimal | None = Field(default=None, gt=0, decimal_places=2)
    category: str | None = Field(
        default=None,
        pattern=r"^(rent|utilities|salaries|suppliers|taxes|marketing|maintenance|financing|other)$",
    )
    transaction_date: date | None = None
    description: str | None = Field(default=None, min_length=2, max_length=500)
    is_recurring: bool | None = None
    supplier_name: str | None = Field(default=None, max_length=300)
    notes: str | None = Field(default=None, max_length=1000)
