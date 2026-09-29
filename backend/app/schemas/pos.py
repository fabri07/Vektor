"""Contrato HTTP de la caja (B3).

Lo que el cliente NO manda: el precio unitario y el total de línea. Los pone el
backend desde el catálogo bloqueado. Un carrito que llega con sus propios
importes es un carrito que puede cobrar de menos, y el cajero no es
necesariamente quien arma la petición —entre el navegador y acá hay una cola
offline que se puede editar—.
"""

from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import UUID

from pydantic import BaseModel, Field, computed_field, field_validator, model_validator

from app.domain import pos_price_token
from app.domain.business_time import a_hora_de_negocio, fecha_de_negocio, today_ar
from app.domain.product import effective_threshold
from app.domain.sale_unit import formatear_cantidad
from app.schemas.transaction import PAYMENT_METHOD_PATTERN

if TYPE_CHECKING:  # pragma: no cover - sólo para tipar el helper del catálogo
    from app.persistence.models.product import Product

_MAX_IMPORTE = Decimal("99999999.99")


class PosLineRequest(BaseModel):
    """Una línea del carrito.

    ``unit_price_ars``, ``product_name``, ``sale_unit``,
    ``base_units_per_sale_unit`` y ``price_token`` son lo que la caja **cobró**,
    con la firma que el propio servidor emitió en el catálogo (ver
    ``domain/pos_price_token``). Van juntos o no van: sin token válido el
    servidor usa el catálogo, como antes de B7. Lo que el cliente sigue sin
    poder declarar es el importe de la línea ni el total: eso lo recalcula
    ``calcular_operacion``.

    El nombre y la unidad se declaran —firmados— porque el ticket tiene que
    decir lo que se vendió **cuando se vendió**: leerlos del catálogo al
    sincronizar hacía que renombrar un producto cambiara el comprobante de una
    venta de ayer, y que cambiarle la unidad invalidara el token de una venta
    legítima ya cobrada.
    """

    product_id: UUID
    #: En UNIDADES BASE: 0,750 kg de un producto en gramos con factor 1000 son 750.
    quantity: int = Field(ge=1)
    unit_price_ars: Decimal | None = Field(
        default=None, gt=0, le=_MAX_IMPORTE, decimal_places=2
    )
    #: 300 como ``Product.name``: con menos se truncaría un nombre válido y la
    #: firma no daría.
    product_name: str | None = Field(default=None, max_length=300)
    sale_unit: str | None = Field(default=None, max_length=20)
    base_units_per_sale_unit: int | None = Field(default=None, ge=1)
    price_token: str | None = Field(default=None, max_length=120)
    #: Descuento monetario de esta línea. Sobre el TOTAL, nunca sobre el unitario.
    discount_ars: Decimal = Field(default=Decimal("0"), ge=0, le=_MAX_IMPORTE, decimal_places=2)

    @model_validator(mode="after")
    def precio_declarado_completo(self) -> "PosLineRequest":
        """Los cinco campos firmados van juntos o no va ninguno.

        Aceptar cuatro de cinco y caer al catálogo por el que falta sería un
        downgrade silencioso: la caja cobró una cosa, el servidor guardó otra y
        nadie se entera.
        """
        declarados = (
            self.unit_price_ars is not None,
            bool(self.product_name),
            bool(self.sale_unit),
            self.base_units_per_sale_unit is not None,
            bool(self.price_token),
        )
        if any(declarados) and not all(declarados):
            raise ValueError(
                "unit_price_ars, product_name, sale_unit, base_units_per_sale_unit "
                "y price_token van juntos."
            )
        return self


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
    #: Cuándo el cajero cerró el ticket y confirmó haber recibido el pago.
    #: No es lo mismo que `operation_date` (la fecha de negocio) ni que cuándo
    #: llegó la petición: una venta cobrada sin red puede sincronizar días
    #: después. Es la afirmación explícita que habilita capturar una salida
    #: pendiente de stock (B11) — no una prueba, un registro fechado.
    charged_at: datetime | None = None
    #: Id del turno de caja que generó el cliente al abrir la caja. B8 lo va a
    #: mapear a la sesión real: sin él, una venta que sincroniza después del
    #: arqueo no se puede atribuir a su turno nunca más.
    client_session_id: str | None = Field(default=None, max_length=64)
    items: list[PosLineRequest] = Field(min_length=1)
    #: Varios pagos = pago mixto (B9). El tope es una restricción, no un campo
    #: con default: no mueve la huella de idempotencia de lo ya encolado. Los
    #: métodos repetidos son válidos; lo que hace mixta a una venta es tener más
    #: de un método DISTINTO.
    tenders: list[PosTenderRequest] = Field(min_length=1, max_length=10)

    @field_validator("operation_date", "charged_at")
    @classmethod
    def fecha_no_futura(cls, v: datetime | None) -> datetime | None:
        # `today_ar()` y no `date.today()`: el server corre en UTC, 3 h adelante
        # de Argentina, así que entre las 21:00 y la medianoche `date.today()`
        # acepta una venta fechada mañana. Una caja vende en esa franja todas
        # las noches.
        if v is None:
            return None
        if fecha_de_negocio(v) > today_ar():
            raise ValueError("date cannot be in the future.")
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
    #: El precio que tenía el catálogo AL SINCRONIZAR, sólo si difería del que
    #: se cobró. `None` = no hubo divergencia (o la venta es anterior a B7).
    unit_price_catalog_ars: Decimal | None = None


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



class PosReceiptLine(BaseModel):
    position: int
    #: Nombre del producto al imprimir. "Producto eliminado" si ya no existe.
    product_name: str
    quantity: int
    #: La cantidad con su unidad ("3 u.", "0,750 kg"): el ticket no hace cuentas.
    quantity_display: str
    unit_price_list: Decimal
    #: Precio × cantidad, antes de cualquier descuento.
    gross_ars: Decimal
    #: SÓLO el descuento propio de la línea. La parte del descuento global va una
    #: vez, en los totales: mostrarla también por línea la contaría dos veces a
    #: la vista y las líneas dejarían de sumar el subtotal impreso.
    discount_line_ars: Decimal
    #: Lo que efectivamente se cobró por la línea (con su parte del global).
    line_total_ars: Decimal


class PosReceiptTender(BaseModel):
    payment_method: str
    amount_ars: Decimal


class PosReceiptResponse(BaseModel):
    """Todo lo que el ticket necesita, ya resuelto por el servidor (B12).

    Trae el nombre del negocio porque el cajero no puede leer `/tenants/me`, y
    los nombres de producto porque el ticket no puede depender del catálogo que
    la caja tenga en memoria. Sin costos: lo imprime y lo lee el cliente.
    """

    id: UUID
    #: Número corto para ubicar el ticket. NO es correlativo: es no fiscal.
    number: str
    business_name: str
    operation_date: datetime
    status: str
    lines: list[PosReceiptLine]
    tenders: list[PosReceiptTender]
    subtotal_ars: Decimal
    discount_ars: Decimal
    total_ars: Decimal
    cash_received_ars: Decimal | None
    cash_change_ars: Decimal | None
    customer_name: str | None
    cashier_name: str | None
    terminal_name: str | None


class PosOperationSummary(BaseModel):
    """Una fila de "Últimos tickets", para reimprimir."""

    id: UUID
    number: str
    operation_date: datetime
    total_ars: Decimal
    status: str
    cashier_name: str | None


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
    #: Firma del servidor sobre precio + unidad + factor (ver
    #: ``domain/pos_price_token``). La caja la guarda con el producto y la
    #: devuelve al cobrar: es lo que le permite declarar el precio que cobró
    #: sin que el servidor tenga que confiar en ella. Lo pone
    #: :func:`a_pos_product`, no ``model_validate``.
    price_token: str | None = None

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


def a_pos_product(producto: "Product", tenant_id: UUID) -> PosProductResponse:
    """La vista de caja de un producto, con su precio firmado.

    Único constructor: los cuatro lugares que devuelven un producto a la caja
    (catálogo, lookup, candidatos de un escaneo ambiguo y el alta de un código
    de barras) pasan por acá. Si alguno construyera el schema a mano, ese
    producto llegaría sin token y su precio no se podría declarar al cobrar.
    """
    respuesta = PosProductResponse.model_validate(producto)
    respuesta.price_token = pos_price_token.emitir(
        tenant_id,
        respuesta.id,
        respuesta.sale_price_ars,
        respuesta.name,
        respuesta.sale_unit,
        respuesta.base_units_per_sale_unit,
    )
    return respuesta


class PosCatalogResponse(BaseModel):
    items: list[PosProductResponse]
    #: El nombre del negocio, para el ticket que la caja imprime SIN RED (B7).
    #: Viaja acá y no en una ruta propia porque el cajero no puede leer
    #: `/tenants/me` y el catálogo se baja igual al abrir la caja.
    business_name: str
    #: Id del último producto de la página. `None` = no hay más. Se pasa como
    #: `after` para pedir la siguiente: orden estable por id, sin saltos ni
    #: repeticiones aunque cambien nombres o precios en el medio.
    next_cursor: UUID | None


class PosCustomerResponse(BaseModel):
    """Sólo lo necesario para elegir a quién se le fía: nada de DNI/CUIT/dirección."""

    id: UUID
    name: str


class PosStockDiscrepancyResponse(BaseModel):
    """Una salida de stock que quedó pendiente (B11).

    `pending_units` es la cantidad COMPLETA de la línea, no el faltante: si
    había 3 y se vendieron 5, quedan 5 unidades pendientes de salir.
    """

    id: UUID
    operation_id: UUID
    product_id: UUID | None
    product_name: str | None
    pending_units: int
    #: Lo que había cuando se capturó. Diagnóstico: el faltante se recalcula al
    #: resolver, porque entre medio pudo haber compras y ventas.
    stock_at_capture: int
    operation_date: datetime
    status: str
    resolution: str | None
    reason: str | None
    resolved_at: datetime | None
    created_at: datetime


class PosResolveDiscrepancyRequest(BaseModel):
    #: `ajuste_de_entrada` (la mercadería estaba) o `anular_ticket` (la venta
    #: está mal). No hay una opción que sólo oculte el aviso: ocultar no
    #: reconcilia nada.
    resolution: str = Field(pattern=r"^(ajuste_de_entrada|anular_ticket)$")
    #: Obligatorio: es lo único que explica después por qué se decidió así.
    reason: str = Field(min_length=1, max_length=300)


class PosTerminalCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)


class PosTerminalResponse(BaseModel):
    """Una caja habilitada. NUNCA trae el secreto."""

    model_config = {"from_attributes": True}

    id: UUID
    name: str
    created_at: datetime
    last_seen_at: datetime | None
    disabled_at: datetime | None


class PosTerminalEnrolledResponse(PosTerminalResponse):
    """La respuesta del alta: la ÚNICA vez que se ve el secreto."""

    secret: str


class PosCashSessionOpenRequest(BaseModel):
    """Abrir turno. El id lo trae la caja: puede haberlo generado sin red."""

    client_session_id: str = Field(min_length=1, max_length=64)
    #: Lo que había en el cajón al abrir. Lo declara quien abre; el servidor no
    #: lo puede saber.
    opening_float_ars: Decimal = Field(
        default=Decimal("0"), ge=0, le=_MAX_IMPORTE, decimal_places=2
    )


class PosCashSessionCloseRequest(BaseModel):
    #: Lo que el cajero CONTÓ en el cajón. Es el dato que el servidor no tiene.
    counted_cash_ars: Decimal = Field(ge=0, le=_MAX_IMPORTE, decimal_places=2)
    notes: str | None = Field(default=None, max_length=500)


class PosLateSaleResponse(BaseModel):
    """Una venta que llegó después del cierre. No movió el arqueo firmado."""

    model_config = {"from_attributes": True}

    operation_id: UUID
    cash_ars: Decimal
    created_at: datetime


class PosCashSessionResponse(BaseModel):
    model_config = {"from_attributes": True}

    id: UUID
    client_session_id: str
    terminal_id: UUID | None
    user_id: UUID | None
    cashier_name: str | None = None
    opening_float_ars: Decimal
    opened_at: datetime
    closed_at: datetime | None
    closed_by_user_id: UUID | None
    #: `None` mientras el turno está abierto: el esperado se congela al cerrar.
    #: Para ver el parcial de un turno abierto está `expected_cash_now_ars`.
    expected_cash_ars: Decimal | None
    counted_cash_ars: Decimal | None
    difference_ars: Decimal | None
    notes: str | None
    status: str
    #: Sólo en el detalle de un turno ABIERTO: lo cobrado hasta ahora. No se
    #: guarda —cambia con cada venta— y no es el arqueo.
    expected_cash_now_ars: Decimal | None = None
    #: Sólo en el detalle de un turno CERRADO que recibió ventas después.
    late_sales: list[PosLateSaleResponse] = []
