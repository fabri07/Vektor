"""Pydantic schemas for product endpoints."""

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field, computed_field

from app.domain.product import effective_threshold


class ProductResponse(BaseModel):
    model_config = {"from_attributes": True}

    id: UUID
    tenant_id: UUID
    name: str
    sku: str | None
    description: str | None
    category: str | None
    sale_price_ars: Decimal
    unit_cost_ars: Decimal | None
    stock_units: int
    # NULL = no configurado (usa DEFAULT_LOW_STOCK_THRESHOLD_UNITS); 0 = umbral explícito
    low_stock_threshold_units: int | None
    is_active: bool
    custom_fields: dict[str, Any] = {}
    deactivated_at: datetime | None = None
    deactivation_reason: str | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def margin_pct(self) -> float | None:
        if self.unit_cost_ars is None or self.sale_price_ars == 0:
            return None
        return float((self.sale_price_ars - self.unit_cost_ars) / self.sale_price_ars * 100)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_low_stock(self) -> bool:
        return self.stock_units <= effective_threshold(self.low_stock_threshold_units)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def stock_status(self) -> str:
        """Estado canónico del producto: in_stock | low_stock | out_of_stock.
        'incoming' se reserva para cuando existan purchase_orders."""
        if self.stock_units == 0:
            return "out_of_stock"
        if self.is_low_stock:
            return "low_stock"
        return "in_stock"


class CreateProductRequest(BaseModel):
    name: str = Field(min_length=1, max_length=300)
    category: str | None = Field(default=None, max_length=100)
    unit_cost_ars: Decimal | None = Field(default=None, gt=0, decimal_places=2)
    sale_price_ars: Decimal = Field(gt=0, decimal_places=2)
    stock_units: int = Field(default=0, ge=0)
    # None = usar DEFAULT_LOW_STOCK_THRESHOLD_UNITS; 0 = explícito, solo sin-stock aplica
    low_stock_threshold_units: int | None = Field(default=None, ge=0)
    sku: str | None = Field(default=None, max_length=100)
    description: str | None = Field(default=None, max_length=1000)
    custom_fields: dict[str, Any] = Field(default_factory=dict)


class UpdateProductRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=300)
    category: str | None = Field(default=None, max_length=100)
    unit_cost_ars: Decimal | None = Field(default=None, gt=0, decimal_places=2)
    sale_price_ars: Decimal | None = Field(default=None, gt=0, decimal_places=2)
    stock_units: int | None = Field(default=None, ge=0)
    low_stock_threshold_units: int | None = Field(default=None, ge=0)
    sku: str | None = Field(default=None, max_length=100)
    is_active: bool | None = None
    custom_fields: dict[str, Any] | None = None
