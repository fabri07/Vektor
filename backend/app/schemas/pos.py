"""Contrato HTTP de la caja (B3).

Lo que el cliente NO manda: el precio unitario y el total de línea. Los pone el
backend desde el catálogo bloqueado. Un carrito que llega con sus propios
importes es un carrito que puede cobrar de menos, y el cajero no es
necesariamente quien arma la petición —entre el navegador y acá hay una cola
offline que se puede editar—.
"""

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, Field, computed_field, field_validator

from app.domain.business_time import a_hora_de_negocio, fecha_de_negocio, today_ar
from app.domain.product import effective_threshold
from app.domain.sale_unit import formatear_cantidad
from app.schemas.transaction import PAYMENT_METHOD_PATTERN

_MAX_IMPORTE = Decimal("99999999.99")


class PosLineRequest(BaseModel):
    """Una línea del carrito. El precio lo resuelve el servidor."""

    product_id: UUID
    #: En UNIDADES BASE: 0,750 kg de un producto en gramos con factor 1000 son 750.
    quantity: int = Field(ge=1)
    #: Descuento monetario de esta línea. Sobre el TOTAL, nunca sobre el unitario.
    discount_ars: Decimal = Field(default=Decimal("0"), ge=0, le=_MAX_IMPORTE, decimal_places=2)


class PosTenderRequest(BaseModel):
    payment_method: str = Field(pattern=PAYMENT_METHOD_PATTERN)
    amount_ars: Decimal = Field(gt=0, le=_MAX_IMPORTE, decimal_places=2)


class PosOperationRequest(BaseModel):
    #: Generado por la caja ANTES del primer envío. Es lo que hace recuperable
    #: una respuesta perdida: sin un id nacido en el cliente, un reintento tras
    #: un corte es indistinguible de una venta nueva.
    client_operation_id: str = Field(min_length=8, max_length=64)
    customer_id: UUID | None = None
    operation_date: datetime
    #: Descuento sobre el total de la venta. Se reparte entre las líneas.
    discount_ars: Decimal = Field(default=Decimal("0"), ge=0, le=_MAX_IMPORTE, decimal_places=2)
    #: Efectivo entregado por el cliente. NO es la venta: sirve para el vuelto.
    cash_received_ars: Decimal | None = Field(default=None, ge=0, le=_MAX_IMPORTE)
    notes: str | None = Field(default=None, max_length=1000)
    items: list[PosLineRequest] = Field(min_length=1)
    tenders: list[PosTenderRequest] = Field(min_length=1)

    @field_validator("operation_date")
    @classmethod
    def operation_date_not_future(cls, v: datetime) -> datetime:
        # `today_ar()` y no `date.today()`: el server corre en UTC, 3 h adelante
        # de Argentina, así que entre las 21:00 y la medianoche `date.today()`
        # acepta una venta fechada mañana. Una caja vende en esa franja todas
        # las noches.
        if fecha_de_negocio(v) > today_ar():
            raise ValueError("operation_date cannot be in the future.")
        return a_hora_de_negocio(v)


class PosVoidRequest(BaseModel):
    #: Por qué se anula. Queda en la auditoría; no cambia qué se revierte.
    reason: str | None = Field(default=None, max_length=300)


class PosOperationLineResponse(BaseModel):
    model_config = {"from_attributes": True}

    id: UUID
    product_id: UUID | None
    sale_entry_id: UUID | None
    position: int
    quantity: int
    unit_price_list: Decimal
    discount_line_ars: Decimal
    #: Cuánto del descuento global le tocó. Sin esto, el total de la línea no
    #: coincide con precio × cantidad y nada explica por qué.
    discount_global_share_ars: Decimal
    line_total_ars: Decimal


class PosTenderResponse(BaseModel):
    model_config = {"from_attributes": True}

    id: UUID
    position: int
    payment_method: str
    amount_ars: Decimal


class PosOperationResponse(BaseModel):
    model_config = {"from_attributes": True}

    id: UUID
    client_operation_id: str
    created_by_user_id: UUID | None
    customer_id: UUID | None
    operation_date: datetime
    subtotal_ars: Decimal
    discount_ars: Decimal
    total_ars: Decimal
    cash_received_ars: Decimal | None
    cash_change_ars: Decimal | None
    status: str
    notes: str | None
    lines: list[PosOperationLineResponse]
    tenders: list[PosTenderResponse]



class PosProductResponse(BaseModel):
    """Un producto como lo ve la CAJA: sin costos, sin márgenes, sin lista.

    `unit_cost_ars`, `margin_pct`, `list_price_ars` y `custom_fields` (que puede
    traer la marca o la procedencia del costo) no están, y el test lo verifica
    sobre las CLAVES de la respuesta: que la pantalla no los muestre no alcanza.

    `stock_units` va en UNIDADES BASE para que la caja offline (B7) descuente;
    `stock_display` es presentación y nunca se parsea para hacer cuentas.
    `sale_price_ars` es por UNIDAD DE VENTA (por kg, por litro, por unidad).
    """

    model_config = {"from_attributes": True}

    id: UUID
    name: str
    internal_sku: str | None = None
    sku: str | None = None
    barcode: str | None = None
    sale_price_ars: Decimal
    sale_unit: str = "unit"
    base_units_per_sale_unit: int = 1
    stock_units: int
    low_stock_threshold_units: int | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def stock_display(self) -> str:
        return formatear_cantidad(self.stock_units, self.sale_unit, self.base_units_per_sale_unit)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def stock_status(self) -> str:
        if self.stock_units == 0:
            return "out_of_stock"
        if self.stock_units <= effective_threshold(self.low_stock_threshold_units):
            return "low_stock"
        return "in_stock"


class PosCatalogResponse(BaseModel):
    items: list[PosProductResponse]
    #: Id del último producto de la página. `None` = no hay más. Se pasa como
    #: `after` para pedir la siguiente: orden estable por id, sin saltos ni
    #: repeticiones aunque cambien nombres o precios en el medio.
    next_cursor: UUID | None


class PosCustomerResponse(BaseModel):
    """Sólo lo necesario para elegir a quién se le fía: nada de DNI/CUIT/dirección."""

    id: UUID
    name: str
