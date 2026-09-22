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

from pydantic import BaseModel, Field, field_validator

from app.domain.business_time import today_ar
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
        if v.date() > today_ar():
            raise ValueError("operation_date cannot be in the future.")
        return v


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
