"""Catálogo de LECTURA de campos por entidad — Cambio 1 del plan de
conservación y acceso a datos de negocio (docs/plans/conservacion-y-acceso-datos-negocio.md).

Qué problema resuelve
---------------------
Un valor de negocio que el import capturó bien puede terminar en tres lugares
distintos según cómo esté modelado: una columna real del ORM (``unit_cost_ars``),
una clave dentro de ``custom_fields`` que un target CANÓNICO escribe ahí a
propósito (``purchase_base_cost``, ``shipping_percentage`` — F-H6.d, auxiliares
del costo que no son el costo final), o una clave de ``custom_fields`` que el
usuario declaró libremente al mapear (``custom_field:{key}``, ya cubierto por
``TenantCustomFieldDefinition``). Sin un catálogo único, cada pantalla decide
por su cuenta qué mostrar y dos de los tres casos quedan invisibles — no
porque el dato no se guardó, sino porque nadie declaró dónde vive.

Este módulo es la identidad ESTABLE de los primeros dos casos (canónico y
evidencia). El tercero (adicional declarado) sigue viviendo en
``field_definition_service.get_merged_definitions`` — este módulo no lo
reemplaza, lo combina (ver ``business_field_catalog_service.py``).

Qué NO hace
-----------
No valida tipos al escribir, no ejecuta código (``value_path`` es una ruta de
datos, nunca un `eval`), y no decide qué entra al margen/costo/caja — eso lo
deciden ``DeterministicFinance``/F-H6.d, con o sin este catálogo. Marcar un
campo como "evidencia" es informativo para la UI, no una regla financiera
nueva.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Origin = Literal["canonical", "evidence"]


def field_id_for(entity_type: str, value_path: str) -> str:
    """Identidad estable = DÓNDE vive el dato, no cómo se llama.

    Dos campos con el mismo `field_key` pero storage distinto (ej.
    `product.name`, la columna real, y un `custom_field:name` que un tenant
    declaró sin saber que colisionaba) son datos DISTINTOS y tienen que
    coexistir — si la identidad se basara en el label o el field_key, uno de
    los dos desaparecería en la deduplicación. Basarla en `value_path`
    resuelve esto por construcción: solo colapsan dos entradas que apuntan
    literalmente al mismo lugar.
    """
    return f"{entity_type}:{value_path}"


@dataclass(frozen=True)
class FieldDescriptor:
    #: `field_id_for(entity_type, value_path)` — ver esa función. Independiente
    #: del label: renombrar un campo (F10 invariant) nunca cambia este id.
    field_id: str
    entity_type: str
    field_key: str
    label: str
    data_type: Literal["text", "number", "date", "boolean", "enum"]
    #: "canonical" = columna real del ORM O campo propio del rubro (aunque su
    #: storage sea `custom_fields`, ej. "Color"/"Estilo"). "evidence" = vive en
    #: `custom_fields` y es target de mapeo de primera clase (F-H6.d), pero no
    #: es un atributo que el rubro declare como propio. "additional" (fuera de
    #: este dataclass, ver el service) = lo declaró el tenant libremente.
    origin: Origin
    #: Dónde vive el valor: nombre de atributo ORM (`"unit_cost_ars"`) o
    #: `"custom_fields.<key>"`. Nunca código ejecutable — solo una ruta. UN
    #: campo del rubro (`VerticalFieldDefinition`) NO implica columna real —
    #: la mayoría vive en `custom_fields` porque el ORM no tiene esa columna.
    value_path: str
    unit: str | None = None
    editable: bool = True
    exportable: bool = True
    searchable: bool = True
    default_visible: bool = True
    #: Explicación legible de una restricción financiera existente (F10/F-H6.d),
    #: si la hay. No es una regla nueva — documenta la que ya rige.
    financial_rule: str | None = None
    enum_options: tuple[dict[str, str], ...] | None = None


#: Campos de PRODUCTO reconocidos por el importador que hoy no tienen una
#: identidad unificada entre import/tabla/exportación. Solo cubre lo que
#: `ingestion_import_service.py` y `models/product.py` ya persisten — agregar
#: un campo acá no lo empieza a guardar, solo lo hace consultable.
PRODUCT_FIELDS: tuple[FieldDescriptor, ...] = (
    FieldDescriptor(
        field_id="product:name",
        entity_type="product",
        field_key="name",
        label="Nombre",
        data_type="text",
        origin="canonical",
        value_path="name",
    ),
    FieldDescriptor(
        field_id="product:sku",
        entity_type="product",
        field_key="sku",
        label="Código (SKU)",
        data_type="text",
        origin="canonical",
        value_path="sku",
    ),
    FieldDescriptor(
        field_id="product:barcode",
        entity_type="product",
        field_key="barcode",
        label="Código de barras (EAN/UPC)",
        data_type="text",
        origin="canonical",
        value_path="barcode",
        default_visible=False,
    ),
    FieldDescriptor(
        field_id="product:category",
        entity_type="product",
        field_key="category",
        label="Categoría",
        data_type="text",
        origin="canonical",
        value_path="category",
    ),
    FieldDescriptor(
        field_id="product:description",
        entity_type="product",
        field_key="description",
        label="Descripción",
        data_type="text",
        origin="canonical",
        value_path="description",
        default_visible=False,
    ),
    FieldDescriptor(
        field_id="product:sale_price_ars",
        entity_type="product",
        field_key="sale_price_ars",
        label="Precio de venta",
        data_type="number",
        origin="canonical",
        value_path="sale_price_ars",
        unit="ARS",
        financial_rule="El único de los tres precios que entra al margen (F10).",
    ),
    FieldDescriptor(
        field_id="product:unit_cost_ars",
        entity_type="product",
        field_key="unit_cost_ars",
        label="Costo unitario",
        data_type="number",
        origin="canonical",
        value_path="unit_cost_ars",
        unit="ARS",
        financial_rule="Costo de referencia vigente; alimenta el margen y el valor de stock.",
    ),
    FieldDescriptor(
        field_id="product:list_price_ars",
        entity_type="product",
        field_key="list_price_ars",
        label="Precio de lista (sugerido)",
        data_type="number",
        origin="canonical",
        value_path="list_price_ars",
        unit="ARS",
        default_visible=False,
        financial_rule="Informativo — NO entra al margen (F10).",
    ),
    FieldDescriptor(
        field_id="product:stock_units",
        entity_type="product",
        field_key="stock_units",
        label="Stock (unidades)",
        data_type="number",
        origin="canonical",
        value_path="stock_units",
    ),
    FieldDescriptor(
        field_id="product:acquired_at",
        entity_type="product",
        field_key="acquired_at",
        label="Fecha de alta/adquisición",
        data_type="date",
        origin="canonical",
        value_path="acquired_at",
        default_visible=False,
    ),
    FieldDescriptor(
        field_id="product:expiry_date",
        entity_type="product",
        field_key="expiry_date",
        label="Fecha de vencimiento",
        data_type="date",
        origin="canonical",
        value_path="expiry_date",
        default_visible=False,
    ),
    FieldDescriptor(
        field_id="product:external_code",
        entity_type="product",
        field_key="external_code",
        label="Código en tu sistema",
        data_type="text",
        origin="canonical",
        value_path="external_code",
        editable=False,  # E6a-B: aditivo desde el import, no se pisa a mano acá.
        default_visible=False,
    ),
    FieldDescriptor(
        field_id="product:external_source",
        entity_type="product",
        field_key="external_source",
        label="Sistema de origen del código",
        data_type="text",
        origin="canonical",
        value_path="external_source",
        editable=False,
        default_visible=False,
    ),
    # ── Evidencia del costo (F-H6.d): viven en custom_fields, son targets de
    # mapeo de primera clase, y nunca se suman al costo final ni al margen.
    FieldDescriptor(
        field_id=field_id_for("product", "custom_fields.purchase_base_cost"),
        entity_type="product",
        field_key="purchase_base_cost",
        label="Precio de compra (costo base, sin envío)",
        data_type="number",
        origin="evidence",
        value_path="custom_fields.purchase_base_cost",
        unit="ARS",
        editable=False,
        financial_rule=(
            "Auxiliar del costo original del archivo — NO es el costo final "
            "(ese es unit_cost_ars) y nunca se suma dos veces."
        ),
    ),
    FieldDescriptor(
        field_id=field_id_for("product", "custom_fields.shipping_percentage"),
        entity_type="product",
        field_key="shipping_percentage",
        label="% de envío sobre el costo base",
        data_type="number",
        origin="evidence",
        value_path="custom_fields.shipping_percentage",
        editable=False,
        default_visible=False,
        financial_rule="Auxiliar del costo original del archivo — no se recalcula ni se resuma.",
    ),
)


#: Campos de VENTA. Verificado contra `models/transaction.py::SaleEntry` — a
#: propósito NO incluye `product_name`/`sku`/`barcode`/`customer_dni`/
#: `customer_cuit`/`customer_email`/`customer_phone`/`customer_name` ni los de
#: identidad de operación (`invoice_number`/`document_type`/`document_series`/
#: `external_operation_id`/`external_line_id`): son targets del CATÁLOGO DE
#: MAPEO (`column_mapping_service.CANONICAL_FIELDS["sale"]`), pero se
#: CONSUMEN al importar — resuelven `product_id`/`customer_id` o alimentan el
#: hash de `domain/operation_identity.py` (`operation_fingerprints`) — y no
#: quedan como un valor propio y legible en la fila de la venta. Copiar la
#: lista del mapeo sin verificar esto habría sido exactamente el error que
#: motivó esta revisión (asumir en vez de comprobar la ruta real).
SALE_FIELDS: tuple[FieldDescriptor, ...] = (
    FieldDescriptor(
        field_id="sale:amount",
        entity_type="sale",
        field_key="amount",
        label="Monto de venta",
        data_type="number",
        origin="canonical",
        value_path="amount",
        unit="ARS",
    ),
    FieldDescriptor(
        field_id="sale:quantity",
        entity_type="sale",
        field_key="quantity",
        label="Cantidad",
        data_type="number",
        origin="canonical",
        value_path="quantity",
    ),
    FieldDescriptor(
        field_id="sale:unit_price",
        entity_type="sale",
        field_key="unit_price",
        label="Precio unitario vendido",
        data_type="number",
        origin="canonical",
        value_path="unit_price",
        unit="ARS",
        default_visible=False,
        financial_rule=(
            "Precio REALMENTE vendido en esta transacción — distinto de "
            "product.sale_price_ars (el vigente configurado). Nunca se deriva "
            "de amount/quantity."
        ),
    ),
    FieldDescriptor(
        field_id="sale:transaction_date",
        entity_type="sale",
        field_key="transaction_date",
        label="Fecha de venta",
        data_type="date",
        origin="canonical",
        value_path="transaction_date",
    ),
    FieldDescriptor(
        field_id="sale:payment_method",
        entity_type="sale",
        field_key="payment_method",
        label="Método de pago",
        data_type="text",
        origin="canonical",
        value_path="payment_method",
    ),
    FieldDescriptor(
        field_id="sale:notes",
        entity_type="sale",
        field_key="notes",
        label="Notas",
        data_type="text",
        origin="canonical",
        value_path="notes",
        default_visible=False,
    ),
)

#: Campos de GASTO. Verificado contra `models/transaction.py::ExpenseEntry` —
#: mismo criterio que sale: `product_name`/`sku`/`barcode`/`supplier_cuil`/
#: `supplier_cuit`/`supplier_email`/`supplier_phone` resuelven `product_id`/
#: `supplier_id` y no quedan en la fila; `quantity`/`unit_price` de una compra
#: no viven en `ExpenseEntry` (no tiene esas columnas) sino en
#: `inventory_movements`, otra entidad — fuera de alcance acá.
#: `shipping_cost`/`shipping_cost_line`/`discount`/`taxes` se CONSUMEN para
#: ajustar el costo de la línea o generan un `ExpenseEntry` DISTINTO (F-H6.a,
#: "Envío de las líneas") — no quedan como valor propio de esta fila tampoco.
EXPENSE_FIELDS: tuple[FieldDescriptor, ...] = (
    FieldDescriptor(
        field_id="expense:amount",
        entity_type="expense",
        field_key="amount",
        label="Monto del gasto",
        data_type="number",
        origin="canonical",
        value_path="amount",
        unit="ARS",
    ),
    FieldDescriptor(
        field_id="expense:category",
        entity_type="expense",
        field_key="category",
        label="Categoría",
        data_type="text",
        origin="canonical",
        value_path="category",
    ),
    FieldDescriptor(
        field_id="expense:expense_type",
        entity_type="expense",
        field_key="expense_type",
        label="Tipo (OPEX/COGS)",
        data_type="text",
        origin="canonical",
        value_path="expense_type",
        editable=False,  # cambia vía reclasificación (efectos propios), no PATCH directo.
        financial_rule="COGS = compra de mercadería, entra al stock; OPEX = gasto operativo.",
    ),
    FieldDescriptor(
        field_id="expense:transaction_date",
        entity_type="expense",
        field_key="transaction_date",
        label="Fecha del gasto",
        data_type="date",
        origin="canonical",
        value_path="transaction_date",
    ),
    FieldDescriptor(
        field_id="expense:description",
        entity_type="expense",
        field_key="description",
        label="Descripción",
        data_type="text",
        origin="canonical",
        value_path="description",
    ),
    FieldDescriptor(
        field_id="expense:is_recurring",
        entity_type="expense",
        field_key="is_recurring",
        label="Recurrente",
        data_type="boolean",
        origin="canonical",
        value_path="is_recurring",
        default_visible=False,
    ),
    FieldDescriptor(
        field_id="expense:payment_method",
        entity_type="expense",
        field_key="payment_method",
        label="Método de pago",
        data_type="text",
        origin="canonical",
        value_path="payment_method",
    ),
    FieldDescriptor(
        field_id="expense:supplier_name",
        entity_type="expense",
        field_key="supplier_name",
        label="Proveedor (texto libre del archivo)",
        data_type="text",
        origin="canonical",
        value_path="supplier_name",
        # Coexiste con el vínculo real `supplier_id` (F7a): texto tal cual lo
        # trajo el archivo, nunca se reemplaza por el nombre del Supplier vinculado.
        financial_rule=None,
    ),
    FieldDescriptor(
        field_id="expense:notes",
        entity_type="expense",
        field_key="notes",
        label="Notas",
        data_type="text",
        origin="canonical",
        value_path="notes",
        default_visible=False,
    ),
    FieldDescriptor(
        field_id=field_id_for("expense", "custom_fields.attributed_to_inventory"),
        entity_type="expense",
        field_key="attributed_to_inventory",
        label="Flete atribuido al costo de inventario",
        data_type="boolean",
        origin="evidence",
        value_path="custom_fields.attributed_to_inventory",
        editable=False,
        default_visible=False,
        financial_rule=(
            "F-H6.d: excluido de los agregados de RESULTADO "
            "(repositories/_expense_scope.py::gasto_de_resultado); incluido en caja."
        ),
    ),
    FieldDescriptor(
        field_id=field_id_for("expense", "custom_fields.profit_withdrawal"),
        entity_type="expense",
        field_key="profit_withdrawal",
        label="Retiro de ganancias",
        data_type="boolean",
        origin="evidence",
        value_path="custom_fields.profit_withdrawal",
        editable=False,
        default_visible=False,
        financial_rule="Marca un gasto PAYROLL/OPEX como retiro del dueño, no un pago a terceros.",
    ),
)

#: Campos de CLIENTE. Verificado contra `models/customer.py`.
CUSTOMER_FIELDS: tuple[FieldDescriptor, ...] = (
    FieldDescriptor(
        field_id="customer:name",
        entity_type="customer",
        field_key="name",
        label="Nombre",
        data_type="text",
        origin="canonical",
        value_path="name",
    ),
    FieldDescriptor(
        field_id="customer:customer_type",
        entity_type="customer",
        field_key="customer_type",
        label="Tipo (persona/empresa)",
        data_type="text",
        origin="canonical",
        value_path="customer_type",
        default_visible=False,
    ),
    FieldDescriptor(
        field_id="customer:last_name",
        entity_type="customer",
        field_key="last_name",
        label="Apellido",
        data_type="text",
        origin="canonical",
        value_path="last_name",
    ),
    FieldDescriptor(
        field_id="customer:doc_type",
        entity_type="customer",
        field_key="doc_type",
        label="Tipo de documento",
        data_type="text",
        origin="canonical",
        value_path="doc_type",
        default_visible=False,
    ),
    FieldDescriptor(
        field_id="customer:dni",
        entity_type="customer",
        field_key="dni",
        label="DNI",
        data_type="text",
        origin="canonical",
        value_path="dni",
    ),
    FieldDescriptor(
        field_id="customer:cuit",
        entity_type="customer",
        field_key="cuit",
        label="CUIT",
        data_type="text",
        origin="canonical",
        value_path="cuit",
    ),
    FieldDescriptor(
        field_id="customer:iva_condition",
        entity_type="customer",
        field_key="iva_condition",
        label="Condición de IVA",
        data_type="text",
        origin="canonical",
        value_path="iva_condition",
        default_visible=False,
    ),
    FieldDescriptor(
        field_id="customer:email",
        entity_type="customer",
        field_key="email",
        label="Email",
        data_type="text",
        origin="canonical",
        value_path="email",
    ),
    FieldDescriptor(
        field_id="customer:phone",
        entity_type="customer",
        field_key="phone",
        label="Teléfono",
        data_type="text",
        origin="canonical",
        value_path="phone",
    ),
    FieldDescriptor(
        field_id="customer:telegram_username",
        entity_type="customer",
        field_key="telegram_username",
        label="Usuario de Telegram",
        data_type="text",
        origin="canonical",
        value_path="telegram_username",
        default_visible=False,
    ),
    FieldDescriptor(
        field_id="customer:address",
        entity_type="customer",
        field_key="address",
        label="Dirección",
        data_type="text",
        origin="canonical",
        value_path="address",
        default_visible=False,
    ),
    FieldDescriptor(
        field_id="customer:locality",
        entity_type="customer",
        field_key="locality",
        label="Localidad",
        data_type="text",
        origin="canonical",
        value_path="locality",
        default_visible=False,
    ),
    FieldDescriptor(
        field_id="customer:province",
        entity_type="customer",
        field_key="province",
        label="Provincia",
        data_type="text",
        origin="canonical",
        value_path="province",
        default_visible=False,
    ),
    FieldDescriptor(
        field_id="customer:postal_code",
        entity_type="customer",
        field_key="postal_code",
        label="Código postal",
        data_type="text",
        origin="canonical",
        value_path="postal_code",
        default_visible=False,
    ),
    FieldDescriptor(
        field_id="customer:birthday",
        entity_type="customer",
        field_key="birthday",
        label="Cumpleaños",
        data_type="date",
        origin="canonical",
        value_path="birthday",
        default_visible=False,
    ),
    FieldDescriptor(
        field_id="customer:credit_limit",
        entity_type="customer",
        field_key="credit_limit",
        label="Límite de crédito",
        data_type="number",
        origin="canonical",
        value_path="credit_limit",
        unit="ARS",
        default_visible=False,
    ),
    FieldDescriptor(
        field_id="customer:notes",
        entity_type="customer",
        field_key="notes",
        label="Notas",
        data_type="text",
        origin="canonical",
        value_path="notes",
        default_visible=False,
    ),
    FieldDescriptor(
        field_id="customer:external_code",
        entity_type="customer",
        field_key="external_code",
        label="Código en tu sistema",
        data_type="text",
        origin="canonical",
        value_path="external_code",
        editable=False,  # E6a: aditivo desde el import, no se pisa a mano acá.
        default_visible=False,
    ),
    FieldDescriptor(
        field_id="customer:external_source",
        entity_type="customer",
        field_key="external_source",
        label="Sistema de origen del código",
        data_type="text",
        origin="canonical",
        value_path="external_source",
        editable=False,
        default_visible=False,
    ),
)

#: Campos de PROVEEDOR. Verificado contra `models/supplier.py`.
SUPPLIER_FIELDS: tuple[FieldDescriptor, ...] = (
    FieldDescriptor(
        field_id="supplier:name",
        entity_type="supplier",
        field_key="name",
        label="Nombre",
        data_type="text",
        origin="canonical",
        value_path="name",
    ),
    FieldDescriptor(
        field_id="supplier:last_name",
        entity_type="supplier",
        field_key="last_name",
        label="Apellido",
        data_type="text",
        origin="canonical",
        value_path="last_name",
        default_visible=False,
    ),
    FieldDescriptor(
        field_id="supplier:cuil",
        entity_type="supplier",
        field_key="cuil",
        label="CUIL",
        data_type="text",
        origin="canonical",
        value_path="cuil",
    ),
    FieldDescriptor(
        field_id="supplier:cuit",
        entity_type="supplier",
        field_key="cuit",
        label="CUIT",
        data_type="text",
        origin="canonical",
        value_path="cuit",
    ),
    FieldDescriptor(
        field_id="supplier:iva_condition",
        entity_type="supplier",
        field_key="iva_condition",
        label="Condición de IVA",
        data_type="text",
        origin="canonical",
        value_path="iva_condition",
        default_visible=False,
    ),
    FieldDescriptor(
        field_id="supplier:payment_method",
        entity_type="supplier",
        field_key="payment_method",
        label="Método de pago",
        data_type="text",
        origin="canonical",
        value_path="payment_method",
        default_visible=False,
    ),
    FieldDescriptor(
        field_id="supplier:email",
        entity_type="supplier",
        field_key="email",
        label="Email",
        data_type="text",
        origin="canonical",
        value_path="email",
    ),
    FieldDescriptor(
        field_id="supplier:phone",
        entity_type="supplier",
        field_key="phone",
        label="Teléfono",
        data_type="text",
        origin="canonical",
        value_path="phone",
    ),
    FieldDescriptor(
        field_id="supplier:catalog_url",
        entity_type="supplier",
        field_key="catalog_url",
        label="Catálogo (URL)",
        data_type="text",
        origin="canonical",
        value_path="catalog_url",
        default_visible=False,
    ),
    FieldDescriptor(
        field_id="supplier:api_url",
        entity_type="supplier",
        field_key="api_url",
        label="API de precios (URL)",
        data_type="text",
        origin="canonical",
        value_path="api_url",
        default_visible=False,
    ),
    FieldDescriptor(
        field_id="supplier:notes",
        entity_type="supplier",
        field_key="notes",
        label="Notas",
        data_type="text",
        origin="canonical",
        value_path="notes",
        default_visible=False,
    ),
    FieldDescriptor(
        field_id="supplier:external_code",
        entity_type="supplier",
        field_key="external_code",
        label="Código en tu sistema",
        data_type="text",
        origin="canonical",
        value_path="external_code",
        editable=False,
        default_visible=False,
    ),
    FieldDescriptor(
        field_id="supplier:external_source",
        entity_type="supplier",
        field_key="external_source",
        label="Sistema de origen del código",
        data_type="text",
        origin="canonical",
        value_path="external_source",
        editable=False,
        default_visible=False,
    ),
)

#: Catálogo por entidad. Las 5 secciones del plan están cubiertas — cada una
#: se verificó contra su propio modelo ORM (no se copiaron los supuestos de
#: Product): sale/expense excluyen a propósito los targets del mapeo que se
#: CONSUMEN al importar (resuelven un vínculo o alimentan otro efecto) en vez
#: de quedar como valor propio de la fila — ver los comentarios de cada tupla.
BUSINESS_FIELD_CATALOG: dict[str, tuple[FieldDescriptor, ...]] = {
    "product": PRODUCT_FIELDS,
    "sale": SALE_FIELDS,
    "expense": EXPENSE_FIELDS,
    "customer": CUSTOMER_FIELDS,
    "supplier": SUPPLIER_FIELDS,
}

#: Entidades válidas para `GET /fields/available` — incluye las que todavía no
#: tienen catálogo propio (devuelven solo los campos adicionales del tenant)
#: para no romper el contrato del endpoint a medida que se van sumando.
AVAILABLE_ENTITY_TYPES: frozenset[str] = frozenset(
    {"product", "sale", "expense", "customer", "supplier"}
)


def descriptors_for(entity_type: str) -> tuple[FieldDescriptor, ...]:
    """Descriptores canónicos/evidencia de una entidad. Vacío si no está cubierta todavía."""
    return BUSINESS_FIELD_CATALOG.get(entity_type, ())
