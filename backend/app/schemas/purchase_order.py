"""Pydantic schemas para purchase_orders."""

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator
from pydantic_core import PydanticCustomError


class PurchaseOrderItem(BaseModel):
    """Un ítem dentro del JSONB ``items`` de PurchaseOrder.

    Contrato fijo — cualquier cambio rompe datos ya persistidos.
    ``quantity`` >= 1; ``unit_cost`` >= 0; ``subtotal`` calculado con Decimal.
    """

    product_id: str | None = None
    product_name: str
    sku: str | None = None
    quantity: int
    unit_cost: Decimal
    subtotal: Decimal

    @field_validator("quantity")
    @classmethod
    def _check_quantity(cls, v: int) -> int:
        if v < 1:
            raise PydanticCustomError("quantity_min", "quantity debe ser >= 1.")
        return v

    @field_validator("unit_cost")
    @classmethod
    def _check_unit_cost(cls, v: Decimal) -> Decimal:
        if v < 0:
            raise PydanticCustomError("unit_cost_negative", "unit_cost debe ser >= 0.")
        return v


class PurchaseOrderResponse(BaseModel):
    """Respuesta pública de un PurchaseOrder."""

    model_config = {"from_attributes": True}

    id: UUID
    tenant_id: UUID
    supplier_id: UUID | None
    status: str
    total: Decimal
    items: list[PurchaseOrderItem] = Field(default_factory=list)
    notes: str | None
    created_at: datetime
