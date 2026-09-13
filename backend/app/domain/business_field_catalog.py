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


@dataclass(frozen=True)
class FieldDescriptor:
    #: Identidad estable: `{entity_type}:{field_key}`. Independiente del label
    #: — renombrar un campo (F10 invariant) nunca cambia este id.
    field_id: str
    entity_type: str
    field_key: str
    label: str
    data_type: Literal["text", "number", "date", "boolean", "enum"]
    #: "canonical" = columna real del ORM. "evidence" = vive en `custom_fields`
    #: pero es un target de mapeo de primera clase, no un adicional libre.
    origin: Origin
    #: Dónde vive el valor: nombre de atributo ORM (`"unit_cost_ars"`) o
    #: `"custom_fields.<key>"`. Nunca código ejecutable — solo una ruta.
    value_path: str
    unit: str | None = None
    editable: bool = True
    exportable: bool = True
    searchable: bool = True
    default_visible: bool = True
    #: Explicación legible de una restricción financiera existente (F10/F-H6.d),
    #: si la hay. No es una regla nueva — documenta la que ya rige.
    financial_rule: str | None = None


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
        field_id="product:purchase_base_cost",
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
        field_id="product:shipping_percentage",
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


#: Catálogo por entidad. Product es el único cubierto hasta ahora — extender
#: sale/expense/customer/supplier es la continuación natural de este mismo
#: patrón (mismo Cambio 1 del plan), no un rediseño.
BUSINESS_FIELD_CATALOG: dict[str, tuple[FieldDescriptor, ...]] = {
    "product": PRODUCT_FIELDS,
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
