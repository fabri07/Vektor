"""Pydantic schemas for supplier endpoints."""

import re
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field, computed_field, field_serializer, field_validator
from pydantic_core import PydanticCustomError

# CUIL/CUIT argentino: ``XX-XXXXXXXX-X`` (guiones opcionales). No se rellena ni
# se valida el dígito verificador — solo el formato; NULL/vacío se aceptan.
_CUIL_RE = re.compile(r"^\d{2}-?\d{8}-?\d$")


def _validate_cuil(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    if not _CUIL_RE.match(cleaned):
        raise PydanticCustomError(
            "cuil_format", "CUIL inválido: formato esperado XX-XXXXXXXX-X."
        )
    return cleaned


class SupplierResponse(BaseModel):
    model_config = {"from_attributes": True}

    id: UUID
    tenant_id: UUID
    name: str
    last_name: str | None = None
    cuil: str | None = None
    payment_method: str | None = None
    email: str | None
    phone: str | None
    notes: str | None
    custom_fields: dict[str, Any] = {}
    created_at: datetime
    deactivated_at: datetime | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_active(self) -> bool:
        return self.deactivated_at is None


class CreateSupplierRequest(BaseModel):
    name: str = Field(min_length=1, max_length=300)
    last_name: str | None = Field(default=None, max_length=200)
    cuil: str | None = Field(default=None, max_length=13)
    payment_method: str | None = Field(default=None, max_length=30)
    email: str | None = Field(default=None, max_length=320)
    phone: str | None = Field(default=None, max_length=50)
    notes: str | None = Field(default=None, max_length=2000)
    custom_fields: dict[str, Any] = Field(default_factory=dict)

    @field_validator("cuil")
    @classmethod
    def _check_cuil(cls, v: str | None) -> str | None:
        return _validate_cuil(v)


class SupplierProductPurchaseResponse(BaseModel):
    """FASE 3: una fila de la tabla "productos comprados a un proveedor"."""

    model_config = {"from_attributes": True}

    product_id: UUID
    name: str
    last_purchase_at: datetime | None
    total_qty: float
    unit_price: Decimal

    @field_serializer("unit_price")
    def _serialize_unit_price(self, v: Decimal) -> float:
        # Convención del repo (ver schemas/transaction.py): serializar Decimal como
        # número evita que el front reciba un string y rompa formatARS / los cálculos.
        return float(v)


class ReceiptLineRequest(BaseModel):
    """Una línea de remito: producto + cantidad + precio unitario. Validación dura.

    Las cotas numéricas se validan con ``field_validator`` (no ``Field(gt=...)``):
    un ``Field`` sobre ``Decimal`` mete el ``Decimal`` de la cota en el ``ctx`` del
    error de validación, que el handler 422 de la app no puede serializar a JSON.
    """

    product_name: str = Field(min_length=1, max_length=300)
    sku: str | None = Field(default=None, max_length=100)
    qty: float
    unit_price: Decimal

    @field_validator("qty")
    @classmethod
    def _check_qty(cls, v: float) -> float:
        if v <= 0:
            raise PydanticCustomError("qty_positive", "qty debe ser > 0.")
        # El pipeline de stock/COGS trabaja con unidades enteras. Rechazar fraccionarios
        # en vez de truncarlos en el endpoint (int(qty)) — truncar perdería la línea
        # silenciosamente (qty en (0,1) → 0 → gasto $0 y sin stock).
        if v != int(v):
            raise PydanticCustomError(
                "qty_integer", "qty debe ser un número entero de unidades."
            )
        return v

    @field_validator("unit_price")
    @classmethod
    def _check_unit_price(cls, v: Decimal) -> Decimal:
        if v < 0:
            raise PydanticCustomError("unit_price_negative", "unit_price debe ser >= 0.")
        return v


class CreateReceiptRequest(BaseModel):
    """FASE 4: remito de mercadería. Fail-closed: moneda ARS explícita, ≥1 línea,
    qty>0, unit_price>=0, shipping>=0. El proveedor viene del path (real, nunca
    sentinela).
    """

    lines: list[ReceiptLineRequest] = Field(min_length=1)
    shipping_cost: Decimal | None = None
    currency: str
    transaction_date: datetime | None = None

    @field_validator("currency")
    @classmethod
    def _check_currency(cls, v: str) -> str:
        if v != "ARS":
            raise PydanticCustomError(
                "currency_unsupported", "Solo se soporta moneda ARS (explícita)."
            )
        return v

    @field_validator("shipping_cost")
    @classmethod
    def _check_shipping(cls, v: Decimal | None) -> Decimal | None:
        if v is not None and v < 0:
            raise PydanticCustomError(
                "shipping_negative", "shipping_cost debe ser >= 0."
            )
        return v


class ReceiptResponse(BaseModel):
    """Summary de un remito importado."""

    lines: int
    products_created: list[UUID]
    expense_ids: list[UUID]
    shipping_expense_id: UUID | None
    total_cogs_ars: Decimal


class ReceiptExtractionLine(BaseModel):
    """Una línea SUGERIDA por la extracción de remito (prellena el formulario).

    A diferencia de ``ReceiptLineRequest`` (el alta), acá ``qty`` puede ser
    fraccionario: es solo una sugerencia para revisar/editar. La validación dura
    (entero, > 0) corre recién al confirmar por ``POST /suppliers/{id}/receipts``.
    """

    product_name: str
    sku: str | None = None
    qty: float
    unit_price: Decimal

    @field_serializer("unit_price")
    def _serialize_unit_price(self, v: Decimal) -> float:
        return float(v)


class ReceiptExtractionResponse(BaseModel):
    """Resultado de leer el archivo de un remito: líneas sugeridas + metadatos.

    NO persiste nada: el alta la confirma el usuario. ``confidence``/``warnings``
    guían la revisión. ``source_upload_id`` se setea si el archivo se guardó.
    """

    lines: list[ReceiptExtractionLine]
    shipping_cost: Decimal | None = None
    currency: str = "ARS"
    confidence: str
    warnings: list[str] = Field(default_factory=list)
    source_upload_id: UUID | None = None

    @field_serializer("shipping_cost")
    def _serialize_shipping(self, v: Decimal | None) -> float | None:
        return float(v) if v is not None else None


class UpdateSupplierRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=300)
    last_name: str | None = Field(default=None, max_length=200)
    cuil: str | None = Field(default=None, max_length=13)
    payment_method: str | None = Field(default=None, max_length=30)
    email: str | None = Field(default=None, max_length=320)
    phone: str | None = Field(default=None, max_length=50)
    notes: str | None = Field(default=None, max_length=2000)
    custom_fields: dict[str, Any] | None = None

    @field_validator("cuil")
    @classmethod
    def _check_cuil(cls, v: str | None) -> str | None:
        return _validate_cuil(v)
