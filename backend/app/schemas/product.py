"""Pydantic schemas for product endpoints."""

from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, Field, computed_field


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
    low_stock_threshold_units: int
    is_active: bool

    @computed_field  # type: ignore[misc]
    @property
    def margin_pct(self) -> float | None:
        if self.unit_cost_ars is None or self.sale_price_ars == 0:
            return None
        return float(
            (self.sale_price_ars - self.unit_cost_ars) / self.sale_price_ars * 100
        )

    @computed_field  # type: ignore[misc]
    @property
    def is_low_stock(self) -> bool:
        return self.stock_units <= self.low_stock_threshold_units


class CreateProductRequest(BaseModel):
    name: str = Field(min_length=1, max_length=300)
    category: str | None = Field(default=None, max_length=100)
    unit_cost_ars: Decimal | None = Field(default=None, gt=0, decimal_places=2)
    sale_price_ars: Decimal = Field(gt=0, decimal_places=2)
    stock_units: int = Field(default=0, ge=0)
    low_stock_threshold_units: int = Field(default=0, ge=0)
    sku: str | None = Field(default=None, max_length=100)
    description: str | None = Field(default=None, max_length=1000)


class UpdateProductRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=300)
    category: str | None = Field(default=None, max_length=100)
    unit_cost_ars: Decimal | None = Field(default=None, gt=0, decimal_places=2)
    sale_price_ars: Decimal | None = Field(default=None, gt=0, decimal_places=2)
    stock_units: int | None = Field(default=None, ge=0)
    low_stock_threshold_units: int | None = Field(default=None, ge=0)
    sku: str | None = Field(default=None, max_length=100)
    is_active: bool | None = None
