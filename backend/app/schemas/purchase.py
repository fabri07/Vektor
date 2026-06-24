"""Schemas de la compra manual de mercadería (comprobante multi-línea)."""

from __future__ import annotations

import math
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

_MAX_AMOUNT = Decimal("999999999")
_PAYMENT_PATTERN = r"^(cash|debit_card|credit_card|transfer|qr|account|other)$"


def _reject_nan_inf(v: Decimal, field_name: str) -> Decimal:
    fv = float(v)
    if math.isnan(fv) or math.isinf(fv):
        raise ValueError(f"{field_name} no puede ser NaN ni Infinity.")
    return v


class PurchaseLine(BaseModel):
    """Una línea del comprobante de compra.

    ``product_id`` presente → restock de un producto existente; ausente → se crea
    un producto nuevo (``name`` y ``category`` requeridos).
    """

    product_id: UUID | None = None
    name: str | None = Field(default=None, max_length=300)
    category: str | None = Field(default=None, max_length=100)
    sku: str | None = Field(default=None, max_length=100)
    description: str | None = Field(default=None, max_length=1000)
    unit_cost: Decimal = Field(gt=0, le=_MAX_AMOUNT, decimal_places=2)
    quantity: int = Field(ge=1)
    sale_price_ars: Decimal = Field(gt=0, le=_MAX_AMOUNT, decimal_places=2)
    # Solo aplica a producto existente: actualizar costo/precio del catálogo.
    update_price: bool = False

    @field_validator("unit_cost", "sale_price_ars")
    @classmethod
    def _no_nan(cls, v: Decimal) -> Decimal:
        return _reject_nan_inf(v, "amount")


class ManualPurchaseRequest(BaseModel):
    supplier_id: UUID
    payment_method: str = Field(pattern=_PAYMENT_PATTERN, default="cash")
    transaction_date: datetime
    lines: list[PurchaseLine] = Field(min_length=1)

    @field_validator("transaction_date")
    @classmethod
    def _not_future(cls, v: datetime) -> datetime:
        if v.date() > date.today():
            raise ValueError("transaction_date cannot be in the future.")
        return v


class PurchaseLineResult(BaseModel):
    product_id: UUID
    product_name: str
    created: bool
    expense_id: UUID
    new_stock_units: int
    margin_pct: float | None


class ManualPurchaseResponse(BaseModel):
    lines: int
    products_created: list[UUID]
    expense_ids: list[UUID]
    total_cogs: float
    results: list[PurchaseLineResult]
    # Eco de metadata útil para el front (no se persiste acá).
    meta: dict[str, Any] = {}
