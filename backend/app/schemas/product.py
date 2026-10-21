"""Pydantic schemas for product endpoints."""

from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, computed_field, field_validator, model_validator

from app.domain.business_time import a_hora_de_negocio, fecha_de_negocio, now_ar_naive, today_ar
from app.domain.costo_vigente import FECHA_FIELD, ORIGEN_FIELD
from app.domain.margen import es_costo_desactualizado, texto_antiguedad_del_costo
from app.domain.product import effective_threshold
from app.domain.sale_unit import formatear_cantidad, validar_unidad
from app.schemas.pos import PosProductResponse


def _validar_acquired_at(v: datetime | None) -> datetime | None:
    """M·2d: la fecha de alta (y la del stock inicial) no puede ser futura.

    Compara sólo el día de negocio, como ``transaction_date``: hoy a cualquier
    hora vale. Devuelve hora argentina sin zona, el formato de la columna.
    """
    if v is None:
        return None
    if fecha_de_negocio(v) > today_ar():
        raise ValueError("acquired_at cannot be in the future.")
    return a_hora_de_negocio(v)


# F6-B4: umbral "próximo a vencer" (días). Documentado y único.
EXPIRY_WARNING_DAYS = 30


class CategorySuggestionResponse(BaseModel):
    """Categoría SUGERIDA para un producto sin categoría (B·3). No se persiste:
    la calcula el endpoint en cada lectura y sólo se fija si el usuario la acepta
    con el PATCH de siempre."""

    code: str
    label: str


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
    # `stock_units` cuenta UNIDADES BASE (gramos, mililitros, unidades). Estos
    # dos dicen cómo leerlo; con el default unit/1 nada del histórico cambia.
    sale_unit: str = "unit"
    base_units_per_sale_unit: int = 1
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
    # B·3: sólo con categoría vacía y confianza alta; si no, null. La llena el
    # endpoint (``products._con_sugerencias``), no el ORM: un frontend viejo la ignora.
    category_suggestion: CategorySuggestionResponse | None = None
    # M·P4: costo promedio ponderado (DATO, no el costo principal: el margen y el
    # valor de stock siguen con unit_cost_ars, el de reposición). Lo llena el
    # listado/detalle (``products._con_sugerencias``) sólo con la compuerta
    # ``avg_cost_display_rollout``; si no, null. Es el último publicado: con un
    # recálculo pendiente no dice «recalculando».
    avg_cost_ars: Decimal | None = None
    avg_cost_estado: Literal["medido", "incompleto", "sin_medicion"] | None = None
    avg_cost_motivo: str | None = None
    #: B·5c: activo y sin stock inicial vivo (``productos_con_apertura_viva``).
    #: ``None`` sólo en respuestas que no lo calculan.
    needs_initial_stock: bool | None = None

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
        # M·3-2: un costo 0 no es «margen 100%»: es costo sin cargar (lo dejan
        # imports viejos; el alta exige > 0). Sin número, con su motivo.
        if self.unit_cost_ars is None or self.unit_cost_ars <= 0 or self.sale_price_ars <= 0:
            return None
        return float((self.sale_price_ars - self.unit_cost_ars) / self.sale_price_ars * 100)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def margin_motivo(self) -> str | None:
        """Por qué no hay margen de lista (``None`` si lo hay)."""
        if self.unit_cost_ars is None:
            return "sin costo cargado"
        if self.unit_cost_ars <= 0:
            return "costo 0: falta cargar el costo"
        if self.sale_price_ars <= 0:
            return "sin precio de venta"
        return None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def costo_antiguedad_dias(self) -> int | None:
        """M·3-4: días desde la fecha del costo vigente (compra o edición) a hoy.

        Sale del sello que deja ``costo_vigente`` en ``custom_fields``; sin sello
        (costo cargado sin fecha, import viejo) = ``None``, antigüedad desconocida.
        """
        sello = (self.custom_fields or {}).get(FECHA_FIELD)
        if self.unit_cost_ars is None or self.unit_cost_ars <= 0 or not sello:
            return None
        try:
            fecha = datetime.fromisoformat(str(sello)).date()
        except ValueError:
            return None
        return max(0, (today_ar() - fecha).days)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def costo_desactualizado(self) -> bool | None:
        """Más de 45 días (``domain/margen.COSTO_DESACTUALIZADO_DIAS``)."""
        return es_costo_desactualizado(self.costo_antiguedad_dias)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def costo_antiguedad_texto(self) -> str | None:
        """«última compra hace 52 días» / «costo cargado hace 3 días»."""
        return texto_antiguedad_del_costo(
            (self.custom_fields or {}).get(ORIGEN_FIELD), self.costo_antiguedad_dias
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def stock_display(self) -> str:
        """El stock como lo lee un humano: '0,750 kg', no '750'.

        Existe porque `stock_units` en unidades base es correcto para la
        aritmética y peligroso en una pantalla: 5 kg se guardan como 5000 y
        alguien los lee como cinco mil kilos. Toda superficie que MUESTRE stock
        tiene que usar esto en vez del entero crudo.
        """
        return formatear_cantidad(self.stock_units, self.sale_unit, self.base_units_per_sale_unit)

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


#: M·P2c: lo que el usuario declara del envío en un precio de compra cargado a
#: mano: lo incluye (``incluido``), no hubo (``no_aplica``), se pagó aparte
#: (``adicional``) o no sabe (``desconocido``). Mismo vocabulario que el
#: contrato de M·1 (``domain/contrato_de_costo.py``).
TratamientoEnvio = Literal["incluido", "no_aplica", "adicional", "desconocido"]


class CreateProductRequest(BaseModel):
    name: str = Field(min_length=1, max_length=300)
    category: str | None = Field(default=None, max_length=100)
    unit_cost_ars: Decimal | None = Field(default=None, gt=0, decimal_places=2)
    sale_price_ars: Decimal = Field(gt=0, decimal_places=2)
    list_price_ars: Decimal | None = Field(default=None, gt=0, decimal_places=2)
    stock_units: int = Field(default=0, ge=0)
    #: M·P2c: si el precio de compra (``unit_cost_ars``) incluye el envío. Es
    #: opcional: sin respuesta queda ``desconocido`` y el margen sale estimado.
    costo_envio: TratamientoEnvio | None = None
    # None = usar DEFAULT_LOW_STOCK_THRESHOLD_UNITS; 0 = explícito, solo sin-stock aplica
    low_stock_threshold_units: int | None = Field(default=None, ge=0)
    sku: str | None = Field(default=None, max_length=100)
    barcode: str | None = Field(default=None, max_length=64)
    expiry_date: date | None = Field(default=None, description="Vencimiento (informativo)")
    description: str | None = Field(default=None, max_length=1000)
    acquired_at: datetime | None = Field(
        default=None,
        description=(
            "Fecha/hora de alta. Con stock_units > 0 es obligatoria: es desde cuándo "
            "el negocio tiene ese stock inicial (M·2d). No puede ser futura."
        ),
    )
    custom_fields: dict[str, Any] = Field(default_factory=dict)
    #: Unidad de venta (B3). Stock y umbral van en UNIDADES BASE (gramos,
    #: mililitros); precio y costo, por unidad de venta (kg, L).
    sale_unit: Literal["unit", "gram", "milliliter"] = "unit"
    base_units_per_sale_unit: int = Field(default=1, ge=1)

    @field_validator("acquired_at")
    @classmethod
    def _acquired_at_no_futura(cls, v: datetime | None) -> datetime | None:
        return _validar_acquired_at(v)

    @model_validator(mode="after")
    def _unidad_valida(self) -> "CreateProductRequest":
        validar_unidad(self.sale_unit, self.base_units_per_sale_unit)
        return self

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
    #: M·P2c: el stock que el usuario veía al editar. Si no coincide con el
    #: actual (entró una venta en el medio) → 409 en vez de pisarla.
    stock_units_esperado: int | None = Field(default=None, ge=0)
    costo_envio: TratamientoEnvio | None = None
    low_stock_threshold_units: int | None = Field(default=None, ge=0)
    sku: str | None = Field(default=None, max_length=100)
    barcode: str | None = Field(default=None, max_length=64)
    expiry_date: date | None = None
    is_active: bool | None = None
    acquired_at: datetime | None = None
    custom_fields: dict[str, Any] | None = None
    sale_unit: Literal["unit", "gram", "milliliter"] | None = None
    base_units_per_sale_unit: int | None = Field(default=None, ge=1)

    @field_validator("acquired_at")
    @classmethod
    def _acquired_at_no_futura(cls, v: datetime | None) -> datetime | None:
        return _validar_acquired_at(v)

    @model_validator(mode="after")
    def _unidad_valida(self) -> "UpdateProductRequest":
        # Van juntos: cambiar uno sin el otro dejaría una combinación que nadie
        # eligió (gramos con factor de unidad, o al revés).
        if (self.sale_unit is None) != (self.base_units_per_sale_unit is None):
            raise ValueError("sale_unit y base_units_per_sale_unit van juntos.")
        if self.sale_unit is not None and self.base_units_per_sale_unit is not None:
            validar_unidad(self.sale_unit, self.base_units_per_sale_unit)
        return self

    @field_validator("name", mode="before")
    @classmethod
    def _strip_name(cls, v: Any) -> Any:
        return v.strip() if isinstance(v, str) else v


class ScanLookupResponse(BaseModel):
    """Resultado de un escaneo que resolvió a UN producto.

    `product` es la vista de CAJA, sin costos ni márgenes (B5): el escaneo lo
    usa un cajero, y el contrato `{code_type, matched_by, product}` de B4 queda
    igual.
    """

    code_type: str
    #: Por qué columna coincidió: ``barcode`` | ``sku`` | ``internal_sku``.
    matched_by: str
    product: PosProductResponse


class LearnBarcodeRequest(BaseModel):
    #: Lo que mandó el lector, tal cual. El servidor lo clasifica.
    barcode: str = Field(min_length=1, max_length=64)


class ConfirmarAperturaRequest(BaseModel):
    """M·2d: desde qué fecha el negocio tiene el stock inicial (la dice el usuario)."""

    fecha: datetime

    @field_validator("fecha")
    @classmethod
    def _no_futura(cls, v: datetime) -> datetime:
        resultado = _validar_acquired_at(v)
        assert resultado is not None
        return resultado


class ConfirmarAperturaDeArchivoRequest(BaseModel):
    """M·2d-1b: la fecha del stock inicial de un archivo.

    ``aceptar_fechas_de_filas`` confirma con SU fecha las aperturas que la traen
    en su fila; ``fecha`` es la del resto (sin ella, y con alguna sin fecha
    propia → 422). Sin aceptarlas, ``fecha`` es obligatoria y vale para todas.
    """

    fecha: datetime | None = None
    aceptar_fechas_de_filas: bool = False

    @field_validator("fecha")
    @classmethod
    def _no_futura(cls, v: datetime | None) -> datetime | None:
        return _validar_acquired_at(v)


class ResumenAperturasDeArchivoResponse(BaseModel):
    """El stock inicial sin confirmar que acaba de cargar un archivo."""

    aperturas: int
    productos: int
    #: En unidades de venta.
    unidades: Decimal
    #: Sin fecha propia: figuran desde la hora de carga hasta que se confirme.
    pendientes: int
    #: Traen la fecha de su fila; se aceptan o se pisan al confirmar.
    con_fecha_de_fila: int
    fecha_de_fila_min: date | None = None
    fecha_de_fila_max: date | None = None
    #: Primer día de ventas o gastos registrado: una SUGERENCIA, nunca la fecha.
    fecha_sugerida: date | None = None


class ConfirmarAperturaResponse(BaseModel):
    aperturas: int
    productos: int
    #: Ventas ya costeadas con el stock inicial y anteriores a la fecha nueva: su
    #: costo no se revalúa (M·2a); se informan para revisarlas.
    ventas_costeadas_antes: int
    venta_ids: list[UUID]


class RegistrarAperturaRequest(BaseModel):
    """B·5c: el stock inicial de un producto que ya existe.

    Es mercadería que compraron el dueño o los socios: va al COSTO (obligatorio)
    y se vende a otro precio. ``stock_units`` en unidades base, como el alta.
    """

    stock_units: int = Field(ge=1)
    unit_cost_ars: Decimal = Field(gt=0, decimal_places=2)
    fecha: datetime
    costo_envio: TratamientoEnvio | None = None

    @field_validator("fecha")
    @classmethod
    def _no_futura(cls, v: datetime) -> datetime:
        resultado = _validar_acquired_at(v)
        assert resultado is not None
        return resultado


class RegistrarAperturaResponse(BaseModel):
    movement_id: UUID
    product_id: UUID
    stock_units: int
    #: Ventas ``sin_costo`` del producto desde la fecha: se costean al confirmar.
    ventas_sin_costo_desde_la_fecha: int


class FilaStockInicial(BaseModel):
    """Una fila de la tabla de stock inicial (B·5c)."""

    nombre: str = Field(min_length=1, max_length=300)
    sku: str | None = Field(default=None, max_length=100)
    barcode: str | None = Field(default=None, max_length=64)
    categoria: str | None = Field(default=None, max_length=100)
    sale_price_ars: Decimal = Field(gt=0, decimal_places=2)
    unit_cost_ars: Decimal = Field(gt=0, decimal_places=2)
    stock_units: int = Field(ge=1)
    costo_envio: TratamientoEnvio | None = None

    @field_validator("nombre", mode="before")
    @classmethod
    def _strip(cls, v: Any) -> Any:
        return v.strip() if isinstance(v, str) else v


MAX_FILAS_STOCK_INICIAL = 200


class LoteStockInicialPreviewRequest(BaseModel):
    filas: list[FilaStockInicial] = Field(min_length=1, max_length=MAX_FILAS_STOCK_INICIAL)


class CoincidenciaDeFila(BaseModel):
    product_id: UUID
    nombre: str
    por: Literal["sku", "barcode", "nombre"]
    necesita_stock_inicial: bool


class FilaPreview(BaseModel):
    fila: int
    errores: list[str]
    coincidencias: list[CoincidenciaDeFila]


class LoteStockInicialPreviewResponse(BaseModel):
    filas: list[FilaPreview]
    preview_token: str


class EleccionDeFila(BaseModel):
    fila: int = Field(ge=0)
    accion: Literal["crear", "apertura_en"]
    product_id: UUID | None = None


class LoteStockInicialRequest(LoteStockInicialPreviewRequest):
    elecciones: list[EleccionDeFila] = Field(default_factory=list)
    preview_token: str = Field(min_length=1, max_length=128)
    fecha: datetime

    @field_validator("fecha")
    @classmethod
    def _no_futura(cls, v: datetime) -> datetime:
        resultado = _validar_acquired_at(v)
        assert resultado is not None
        return resultado


class ResultadoDeFila(BaseModel):
    fila: int
    accion: Literal["crear", "apertura_en"]
    product_id: UUID
    movement_id: UUID
    #: ``apertura_en`` carga sólo el stock inicial: lo que la fila traía distinto
    #: del producto (sku, barcode, precio, categoría, nombre) NO se aplicó.
    campos_no_aplicados: list[str] = Field(default_factory=list)


class LoteStockInicialResponse(BaseModel):
    filas: list[ResultadoDeFila]
    creados: int
    con_apertura: int
