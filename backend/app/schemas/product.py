"""Pydantic schemas for product endpoints."""

from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field, computed_field, field_validator

from app.domain.business_time import now_ar_naive
from app.domain.product import effective_threshold

# F6-B4: umbral "próximo a vencer" (días). Documentado y único.
EXPIRY_WARNING_DAYS = 30


class ProductResponse(BaseModel):
    model_config = {"from_attributes": True}

    id: UUID
    tenant_id: UUID
    name: str
    # El código que aporta el ARCHIVO o el proveedor. Puede faltar.
    sku: str | None
    # El código propio de Véktor, generado al crear el producto e inmutable.
    # Existe para los productos que el archivo trae sin `sku` — que fueron los
    # 398 del catálogo que lo motivó. `None` sólo en filas anteriores a la
    # migración `20260903_0001`, que no backfillea.
    internal_sku: str | None = None
    barcode: str | None = None
    expiry_date: date | None = None
    description: str | None
    category: str | None
    sale_price_ars: Decimal
    # Sugerido por proveedor/lista. Informativo: NO entra al margen, que sigue
    # calculándose con sale_price_ars (vigente) − unit_cost_ars (costo).
    list_price_ars: Decimal | None = None
    unit_cost_ars: Decimal | None
    stock_units: int
    # NULL = no configurado (usa DEFAULT_LOW_STOCK_THRESHOLD_UNITS); 0 = umbral explícito
    low_stock_threshold_units: int | None
    is_active: bool
    # FASE 3 (B2): producto auto-creado por import al que le faltan precio/costo.
    requires_completion: bool = False
    custom_fields: dict[str, Any] = {}
    # Fecha de alta editable; el frontend cae a created_at cuando es None.
    acquired_at: datetime | None = None
    created_at: datetime
    deactivated_at: datetime | None = None
    deactivation_reason: str | None = None

    @field_validator("custom_fields", mode="before")
    @classmethod
    def _coerce_null_custom_fields(cls, v: Any) -> Any:
        """Un import de catálogo pudo persistir ``custom_fields`` como JSON ``null``
        (``'null'::jsonb`` → ``None`` en Python). El listado NO debe romperse con 503
        por eso: normalizamos ``None`` → ``{}`` al serializar la respuesta."""
        return {} if v is None else v

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

    @computed_field  # type: ignore[prop-decorator]
    @property
    def expiry_status(self) -> str | None:
        """F6-B4: estado de vencimiento a NIVEL PRODUCTO (no por lote — FEFO real
        necesita inventory_lots, fuera de alcance). Informativo: NO infiere la
        cantidad afectada. Valores:
        - ``None``: sin vencimiento conocido (indistinguible de "no perecedero").
        - ``"expired"``: ya venció.
        - ``"expiring_soon"``: vence dentro de EXPIRY_WARNING_DAYS (30) días.
        - ``"ok"``: falta más que el umbral.
        Compara contra "hoy" en hora AR (no UTC)."""
        if self.expiry_date is None:
            return None
        today = now_ar_naive().date()
        if self.expiry_date < today:
            return "expired"
        if self.expiry_date <= today + timedelta(days=EXPIRY_WARNING_DAYS):
            return "expiring_soon"
        return "ok"


class CreateProductRequest(BaseModel):
    name: str = Field(min_length=1, max_length=300)
    category: str | None = Field(default=None, max_length=100)
    unit_cost_ars: Decimal | None = Field(default=None, gt=0, decimal_places=2)
    sale_price_ars: Decimal = Field(gt=0, decimal_places=2)
    list_price_ars: Decimal | None = Field(default=None, gt=0, decimal_places=2)
    stock_units: int = Field(default=0, ge=0)
    # None = usar DEFAULT_LOW_STOCK_THRESHOLD_UNITS; 0 = explícito, solo sin-stock aplica
    low_stock_threshold_units: int | None = Field(default=None, ge=0)
    sku: str | None = Field(default=None, max_length=100)
    barcode: str | None = Field(default=None, max_length=64)
    expiry_date: date | None = Field(default=None, description="Vencimiento (informativo)")
    description: str | None = Field(default=None, max_length=1000)
    acquired_at: datetime | None = Field(default=None, description="Fecha/hora de alta")
    custom_fields: dict[str, Any] = Field(default_factory=dict)

    @field_validator("name", mode="before")
    @classmethod
    def _strip_name(cls, v: Any) -> Any:
        """``min_length=1`` cuenta longitud CRUDA: un nombre de solo espacios
        pasaría la validación y el listener de identidad lo normalizaría a
        ``""`` (F8d). Se hace strip ANTES del chequeo de longitud para que
        ``min_length`` rechace lo que en la práctica es un nombre vacío."""
        return v.strip() if isinstance(v, str) else v


class UpdateProductRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=300)
    category: str | None = Field(default=None, max_length=100)
    unit_cost_ars: Decimal | None = Field(default=None, gt=0, decimal_places=2)
    sale_price_ars: Decimal | None = Field(default=None, gt=0, decimal_places=2)
    list_price_ars: Decimal | None = Field(default=None, gt=0, decimal_places=2)
    stock_units: int | None = Field(default=None, ge=0)
    low_stock_threshold_units: int | None = Field(default=None, ge=0)
    sku: str | None = Field(default=None, max_length=100)
    barcode: str | None = Field(default=None, max_length=64)
    expiry_date: date | None = None
    is_active: bool | None = None
    acquired_at: datetime | None = None
    custom_fields: dict[str, Any] | None = None

    @field_validator("name", mode="before")
    @classmethod
    def _strip_name(cls, v: Any) -> Any:
        return v.strip() if isinstance(v, str) else v
