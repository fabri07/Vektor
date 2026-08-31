"""ColumnMappingService: sugerencias de mapeo de columnas + aprendizaje por tenant."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.header_keys import custom_field_slug, fold_header, match_key
from app.domain.header_semantics import analyze_header

# F-C.c3: los conjuntos que definen "esta hoja mueve unidades" ya viven en el
# dominio y se importan, incluido el guión bajo. Copiarlos acá para no importar un
# privado sería el peor de los dos males: dos definiciones de la misma regla que
# nadie obliga a moverse juntas, que es exactamente lo que pasó con el catálogo de
# campos duplicado en el frontend (incidente ASTERIA). El test
# `test_conditional_requirements.py` clava la IDENTIDAD de los objetos, así que una
# copia futura pone la suite en rojo.
from app.domain.inventory_effect import (
    _PRODUCT_FIELDS,
    _QUANTITY_FIELDS,
    SheetInventoryProfile,
)
from app.domain.text_norm import repair_mojibake
from app.observability.logger import get_logger

logger = get_logger(__name__)

# ── Campos canónicos por entity_type ─────────────────────────────────────────
CANONICAL_FIELDS: dict[str, dict[str, str]] = {
    "sale": {
        "amount": "Monto de venta",
        "transaction_date": "Fecha de venta",
        "quantity": "Cantidad",
        # Precio realmente vendido en esta transacción. NO se deriva de
        # amount/quantity (ver models/transaction.py).
        "unit_price": "Precio unitario vendido",
        "payment_method": "Método de pago",
        "product_name": "Nombre del producto",
        # Identifican el producto de la línea, igual que en `expense` — sin
        # esto, cuando la detección automática de SKU falla (columna genérica
        # "ID", sin ninguno de los alias de `_SKU_COLS`), el usuario no tiene
        # forma de corregir el mapeo a mano en una hoja de ventas.
        "sku": "Código (SKU)",
        "barcode": "Código de barras (EAN/UPC)",
        "notes": "Notas",
        # F7a: campos de referencia al cliente (aditivo — el mapeo/vinculación real
        # a un Customer existente queda para 7c; acá solo se abre el contrato).
        # Prefijo "Cliente — " a propósito: en un select largo agrupa visualmente
        # los campos de referencia. Es el label que ya estaba en la UI antes de
        # que el catálogo pasara a servirse desde acá.
        "customer_dni": "Cliente — DNI",
        "customer_cuit": "Cliente — CUIT",
        "customer_email": "Cliente — Email",
        "customer_phone": "Cliente — Teléfono",
        "customer_name": "Cliente — Nombre",
    },
    "expense": {
        "amount": "Monto del gasto",
        "expense_date": "Fecha del gasto",
        "category": "Categoría",
        "payment_method": "Método de pago",
        "is_recurring": "Recurrente",
        "supplier_name": "Proveedor",
        "notes": "Notas",
        # F-H6.a: una planilla de compras no podía mapear cantidad ni precio
        # unitario, y ésa es la causa de que el costo entre mal — el importador
        # los leía sólo por heurística de headers o no los leía. Con el target
        # explícito, un libro de compras declara qué columna es cada cosa, igual
        # que una hoja de ventas.
        "quantity": "Cantidad",
        # Precio de cada unidad EN ESTA COMPRA. No es el costo de referencia del
        # producto (`unit_cost_ars`) ni el precio de lista: vive en el movimiento
        # (`inventory_movements.unit_cost`) y los dos coexisten.
        "unit_price": "Precio unitario de compra",
        # Identifican el producto de la línea. Sin ellos la compra no se puede
        # vincular ni dar de alta con identidad propia.
        "product_name": "Nombre del producto",
        "sku": "Código (SKU)",
        "barcode": "Código de barras (EAN/UPC)",
        # F-H6.b: identidad de la OPERACIÓN, no del producto. Es lo que permite
        # afirmar que varias filas pertenecen al mismo remito y, por lo tanto,
        # que comparten un envío. Sin esto no se puede agrupar nada.
        "invoice_number": "Número de comprobante / factura",
        # Costo compartido de la operación. Se cobra UNA vez por comprobante:
        # una planilla repite el mismo flete en cada línea del remito.
        "shipping_cost": "Envío / flete",
        # F-M.7: el flete que el ARCHIVO ya asignó a cada línea. Semántica OPUESTA
        # a `shipping_cost` y por eso es un campo distinto: aquél se cobra una vez
        # por comprobante (la cifra repetida se colapsa), éste se SUMA, porque el
        # reparto lo hizo quien armó la planilla. Fusionarlos obligaría a repartir
        # algo que ya venía repartido — ver `plan_line_shipping` en
        # `domain/purchase_shipping.py`.
        "shipping_cost_line": "Envío ya asignado a esta línea",
        # Los dos ajustan el costo de la línea. Que se apliquen o no lo decide el
        # usuario (`BASE_INCLUYE` / `BASE_APLICAR` en `domain/purchase_cost.py`):
        # restarle un descuento a un total que ya lo tiene descontado lo cuenta
        # dos veces, y eso no se adivina desde el encabezado.
        "discount": "Descuento de la línea",
        "taxes": "Impuestos de la línea",
        # F7a: campos de referencia al proveedor (aditivo, ver nota de sale arriba).
        # Ver la nota de los campos de cliente: mismo criterio de agrupación.
        "supplier_cuil": "Proveedor — CUIL",
        "supplier_email": "Proveedor — Email",
        "supplier_phone": "Proveedor — Teléfono",
    },
    # F7a: maestro de CLIENTES — campos que persiste el modelo Customer.
    "customer": {
        "customer_type": "Tipo (persona/empresa)",
        "name": "Nombre",
        "last_name": "Apellido",
        "doc_type": "Tipo de documento",
        "dni": "DNI",
        "cuit": "CUIT",
        "iva_condition": "Condición de IVA",
        "email": "Email",
        "phone": "Teléfono",
        "address": "Dirección",
        "locality": "Localidad",
        "province": "Provincia",
        "postal_code": "Código postal",
        "birthday": "Cumpleaños",
        "notes": "Notas",
    },
    # F7a: maestro de PROVEEDORES — ACOTADO a lo que persiste el modelo Supplier
    # HOY (models/supplier.py). No se agregan doc_type/address/locality/province/
    # postal_code/iva_condition: el modelo no los tiene, quedan fuera de esta PR.
    "supplier": {
        "name": "Nombre",
        "last_name": "Apellido",
        "cuil": "CUIL",
        "cuit": "CUIT",
        "iva_condition": "Condición de IVA",
        "payment_method": "Método de pago",
        "email": "Email",
        "phone": "Teléfono",
        "notes": "Notas",
    },
    "product": {
        "sku": "Código (SKU)",
        "barcode": "Código de barras (EAN/UPC)",
        "name": "Nombre",
        # Los tres precios son conceptos distintos y coexisten — ver la nota en
        # models/product.py. El precio REALMENTE vendido no es ninguno de estos:
        # va en sale.unit_price.
        "sale_price_ars": "Precio de venta",
        "list_price_ars": "Precio de lista (sugerido)",
        "unit_cost_ars": "Costo unitario",
        # Bloque 3A: auxiliares del costo — NO son el costo final que usa el
        # margen (ese es `unit_cost_ars`), se guardan en `custom_fields` para
        # no perder el dato de origen cuando el archivo trae compra+envío ya
        # calculado (F-H6 no corre sobre catálogo; ver ingestion_import_service).
        "purchase_base_cost": "Precio de compra (costo base, sin envío)",
        "shipping_percentage": "% de envío sobre el costo base",
        "stock_units": "Stock (unidades)",
        "category": "Categoría",
        "description": "Descripción",
        "acquired_at": "Fecha de alta/adquisición",
        "expiry_date": "Fecha de vencimiento",
    },
}

# Campos mínimos requeridos por entity_type
REQUIRED_FIELDS: dict[str, list[str]] = {
    "sale": ["amount", "transaction_date"],
    "expense": ["amount", "expense_date"],
    "product": ["name"],
    "customer": ["name"],
    "supplier": ["name"],
}

# F-H4: qué OTRO conjunto de campos cubre un requerido. `amount OR (unit_price
# AND quantity)`: si el archivo trae el precio unitario y la cantidad, el total
# es una cuenta y exigir la columna obligaba a reescribir la planilla antes de
# subirla — la queja que originó este programa.
#
# La alternativa tiene que ser COMPLETA: con sólo el precio o sólo la cantidad no
# hay nada que calcular. Y sólo cuenta lo mapeado a campos CANÓNICOS, por la
# misma razón que el requerido: un `custom_field:` guarda el dato pero no lo
# vuelve un precio unitario para el importador.
#
# Vive acá, al lado de `REQUIRED_FIELDS`, y la sirve el catálogo
# (`GET /ingestion/field-catalog`): el frontend NO puede tener su propia copia.
# La copia que existía divergió una vez y la pantalla terminó mostrando una cosa
# y mandando otra (incidente ASTERIA).
REQUIRED_ALTERNATIVES: dict[str, dict[str, frozenset[str]]] = {
    "sale": {"amount": frozenset({"unit_price", "quantity"})},
    # F-H6.a: la misma regla para compras. F-H4 dejó `expense` afuera porque no
    # tenía `unit_price` ni `quantity` en su catálogo —derivar desde columnas
    # autodetectadas es lo que F10 prohíbe—; ahora los tiene y una línea de compra
    # con precio y cantidad tampoco necesita que le escriban el total.
    "expense": {"amount": frozenset({"unit_price", "quantity"})},
}

# F-C: POR QUÉ el importador necesita cada campo. Vive del lado del backend
# porque es CONSECUENCIA de una regla del importador, no una opinión de la UI: si
# mañana una fila sin fecha deja de ir a "Otros", el texto tiene que cambiar acá y
# no en una pantalla.
#
# Dos reglas de redacción, y las dos nacen de la misma queja: la pantalla decía
# «Campos requeridos sin mapear: transaction_date», que se lee como "esta columna
# de tu planilla es obligatoria" cuando lo que pasa es al revés — Véktor necesita
# que ALGUNA columna le diga la fecha, y le da lo mismo cómo se llame.
#
# 1. Se redacta como CONSECUENCIA, nunca como imperativo. "Véktor necesita saber
#    qué columna contiene la fecha", no "la fecha es obligatoria".
# 2. El motivo no puede afirmar lo que el importador no hace. Los destinos son
#    tres y distintos, verificados contra `ingestion_import_service`:
#      · venta sin monto/sin fecha y gasto sin fecha → van a "Otros" con el motivo
#        (`_capture_unclassified`), o sea que la fila se puede rescatar;
#      · gasto sin monto y producto sin nombre → se DESCARTAN, no queda rastro
#        (`_add_expense`/`_add_product` devuelven `False`);
#      · cliente/proveedor sin nombre → se saltea y se cuenta como inválido en el
#        resumen del archivo (`customer_import_service._validate_record`).
#    Prometer "Otros" donde el importador descarta es peor que no explicar nada.
REQUIRED_REASONS: dict[str, dict[str, str]] = {
    "sale": {
        "amount": (
            "Para registrar una venta, Véktor necesita saber cuánta plata entró. "
            "La fila que no lo traiga —ni el precio unitario y la cantidad para "
            "calcularlo— no se registra como venta: queda en «Otros» con el motivo, "
            "para completarla desde ahí."
        ),
        "transaction_date": (
            "Para importar ventas, Véktor necesita saber qué columna contiene la "
            "fecha: es lo que ubica cada venta en su período. La fila con una fecha "
            "que no se pueda leer queda en «Otros» — nunca se le pone la de hoy."
        ),
        "unit_price": (
            "Reemplaza al monto junto con la cantidad: si la planilla no trae una "
            "columna de total, Véktor lo calcula con estos dos. Con uno solo no hay "
            "nada que calcular."
        ),
        "quantity": (
            "Reemplaza al monto junto con el precio unitario: si la planilla no trae "
            "una columna de total, Véktor lo calcula con estos dos. Con uno solo no "
            "hay nada que calcular."
        ),
    },
    "expense": {
        "amount": (
            "Para registrar un gasto o una compra, Véktor necesita saber cuánta plata "
            "salió. La fila que no lo traiga —ni el precio unitario y la cantidad para "
            "calcularlo— se descarta: no se registra el gasto y tampoco queda en "
            "«Otros»."
        ),
        "expense_date": (
            "Para importar gastos y compras, Véktor necesita saber qué columna "
            "contiene la fecha: es lo que ubica cada gasto en su período. La fila con "
            "una fecha que no se pueda leer queda en «Otros» — nunca se le pone la de "
            "hoy."
        ),
        "unit_price": (
            "Reemplaza al monto junto con la cantidad: si la planilla de compras no "
            "trae el total de la línea, Véktor lo calcula con estos dos. Con uno solo "
            "no hay nada que calcular."
        ),
        "quantity": (
            "Reemplaza al monto junto con el precio unitario: si la planilla de "
            "compras no trae el total de la línea, Véktor lo calcula con estos dos. "
            "Con uno solo no hay nada que calcular."
        ),
    },
    "product": {
        "name": (
            "Es con lo que Véktor identifica al artículo y lo cruza con las ventas y "
            "las compras. La fila sin nombre no crea ni actualiza ningún producto y "
            "tampoco queda en «Otros»: se descarta."
        ),
    },
    "customer": {
        "name": (
            "Nombre o razón social: es con lo que el cliente aparece en Véktor y lo "
            "que permite reconocerlo cuando vuelve a comprar. La fila sin nombre no se "
            "importa y se cuenta como inválida en el resumen del archivo."
        ),
    },
    "supplier": {
        "name": (
            "Nombre o razón social: es con lo que el proveedor aparece en Véktor y lo "
            "que permite agrupar sus compras. La fila sin nombre no se importa y se "
            "cuenta como inválida en el resumen del archivo."
        ),
    },
}


def required_reason(entity_type: str, field: str) -> str:
    """Por qué el importador necesita ``field``, o ``""`` si no hay motivo escrito.

    Cadena vacía y no ``None``: el catálogo lo sirve tal cual y un campo sin
    motivo tiene que renderizar nada, no la palabra "None". Que un requerido se
    quede sin motivo lo caza el test compuerta, no este helper.
    """
    return REQUIRED_REASONS.get(entity_type, {}).get(field, "")


def missing_required_fields(entity_type: str, mapped: set[str]) -> set[str]:
    """Requeridos que ``mapped`` no cubre, ni directo ni por alternativa.

    ``mapped`` son los targets CANÓNICOS mapeados (el caller filtra los
    ``custom_field:`` y los cruzados). Fuente única: la validación del confirm y
    el catálogo que consume la UI llaman acá, así que no pueden discrepar sobre
    si una hoja se puede importar.
    """
    alternativas = REQUIRED_ALTERNATIVES.get(entity_type, {})
    return {
        campo
        for campo in REQUIRED_FIELDS.get(entity_type, [])
        if campo not in mapped
        and not (campo in alternativas and alternativas[campo] <= mapped)
    }


# ── F-C.c3: "obligatorio" es contextual ──────────────────────────────────────
# `required: bool` contesta una pregunta sola para todos los archivos, y por eso
# contesta mal en los dos sentidos: dice que el monto de una venta es obligatorio
# cuando la planilla trae precio × cantidad, y no dice nada del producto en una
# hoja que sí mueve inventario.
#
# Esto lo DESCRIBE, no lo cambia. El booleano queda igual, `REQUIRED_FIELDS` no
# crece y `missing_required_fields` —lo que el confirm valida de verdad— no se
# toca. Volver bloqueante "producto si la venta es inventariable" rechazaría con
# 422 toda planilla de servicios u honorarios que hoy entra bien; la UI puede
# explicar la regla sin que el importador la imponga.
RequirementCondition = Literal["covered_by_alternative", "sheet_moves_units"]

#: El campo no hace falta si OTRO conjunto de campos lo cubre. Cuál es ese
#: conjunto NO se escribe acá: vive en `REQUIRED_ALTERNATIVES` y lo evalúa
#: `missing_required_fields`. Una tercera copia de `{unit_price, quantity}` sería
#: una tercera cosa para mantener sincronizada.
COVERED_BY_ALTERNATIVE: RequirementCondition = "covered_by_alternative"
#: El campo sólo hace falta si las filas de la hoja mueven unidades de un producto
#: identificable. La definición es `SheetInventoryProfile.moves_units`.
SHEET_MOVES_UNITS: RequirementCondition = "sheet_moves_units"


@dataclass(frozen=True)
class ConditionalRequirement:
    """Por qué un campo puede hacer falta en una hoja y no en la de al lado."""

    condition: RequirementCondition
    #: Copy en castellano, para el catálogo y el banner de faltantes.
    explanation: str
    #: Conjuntos de campos que gobiernan la condición, para que la pantalla pueda
    #: nombrar las columnas involucradas. Son LOS MISMOS OBJETOS del dominio, no
    #: copias — y nadie los evalúa acá: la autoridad sigue siendo `moves_units`.
    #: Vacío en `covered_by_alternative`, donde la fuente es `REQUIRED_ALTERNATIVES`.
    signals: tuple[frozenset[str], ...] = ()


CONDITIONAL_REQUIREMENTS: dict[str, dict[str, ConditionalRequirement]] = {
    "sale": {
        "amount": ConditionalRequirement(
            condition=COVERED_BY_ALTERNATIVE,
            explanation=(
                "Sólo hace falta si la planilla no trae el precio unitario y la "
                "cantidad: con esos dos, Véktor calcula el total de cada línea."
            ),
        ),
        "product_name": ConditionalRequirement(
            condition=SHEET_MOVES_UNITS,
            explanation=(
                "Sólo hace falta si las filas mueven unidades de un producto. Sin una "
                "columna que lo identifique, la venta se registra igual, pero Véktor "
                "no puede decir qué se vendió ni proyectar el impacto en el "
                "inventario. Una venta de servicios u honorarios no identifica "
                "ningún artículo y no necesita esta columna."
            ),
            signals=(_PRODUCT_FIELDS, _QUANTITY_FIELDS),
        ),
        "quantity": ConditionalRequirement(
            condition=SHEET_MOVES_UNITS,
            explanation=(
                "Sólo hace falta si las filas mueven unidades de un producto: es lo "
                "que dice cuántas. Una hoja que identifica el artículo pero no trae "
                "cantidades no puede decir nada del inventario."
            ),
            signals=(_PRODUCT_FIELDS, _QUANTITY_FIELDS),
        ),
    },
    "expense": {
        "amount": ConditionalRequirement(
            condition=COVERED_BY_ALTERNATIVE,
            explanation=(
                "Sólo hace falta si la planilla de compras no trae el precio unitario "
                "y la cantidad: con esos dos, Véktor calcula el total de cada línea."
            ),
        ),
        # Las mismas dos reglas valen para una hoja de compras: el dominio no
        # distingue —`default_effect_for` trata `sale` y `expense` igual— y una
        # compra de mercadería sin nombre no da de alta el producto ni suma
        # unidades (`_is_merch_purchase` pide nombre Y cantidad > 0).
        "product_name": ConditionalRequirement(
            condition=SHEET_MOVES_UNITS,
            explanation=(
                "Sólo hace falta si las filas mueven unidades de un producto. Sin una "
                "columna que lo identifique, el gasto se registra igual, pero la "
                "compra no da de alta el artículo ni le suma stock. Un alquiler o un "
                "servicio no necesitan esta columna."
            ),
            signals=(_PRODUCT_FIELDS, _QUANTITY_FIELDS),
        ),
        "quantity": ConditionalRequirement(
            condition=SHEET_MOVES_UNITS,
            explanation=(
                "Sólo hace falta si las filas mueven unidades de un producto: es lo "
                "que dice cuántas entraron. Una compra sin cantidad se registra como "
                "gasto y no toca el inventario."
            ),
            signals=(_PRODUCT_FIELDS, _QUANTITY_FIELDS),
        ),
    },
}


def conditional_requirement(entity_type: str, field: str) -> ConditionalRequirement | None:
    """La regla contextual de ``field``, o ``None`` si el campo no tiene ninguna."""
    return CONDITIONAL_REQUIREMENTS.get(entity_type, {}).get(field)


def requirement_applies(entity_type: str, field: str, mapped: set[str]) -> bool:
    """¿Este campo hace falta en una hoja que ya mapeó ``mapped``?

    **Es DESCRIPTIVO.** No lo llama la validación del confirm: eso sigue siendo
    `missing_required_fields`, que sólo mira `REQUIRED_FIELDS`. Cablearlo a un 422
    convertiría "producto en una hoja inventariable" en un rechazo nuevo, que es
    justo lo que F-C decidió no hacer.
    """
    req = conditional_requirement(entity_type, field)
    if req is None:
        return field in REQUIRED_FIELDS.get(entity_type, [])
    if req.condition == COVERED_BY_ALTERNATIVE:
        # Sin reimplementar la alternativa: se le pregunta a la misma función que
        # valida el confirm, sacando el campo de lo mapeado para que la respuesta
        # sea "¿lo necesita?" y no "¿ya lo tiene?".
        return field in missing_required_fields(entity_type, mapped - {field})
    # `moves_units` es "identifica un producto Y trae cantidad". La conjunción no
    # se reescribe acá: se le pregunta dos veces al dominio. El campo hace falta
    # cuando la hoja todavía no mueve unidades y mapearlo alcanzaría para que sí
    # —o sea, cuando es la mitad que falta—. En una hoja de servicios ninguna de
    # las dos mitades alcanza sola, así que ninguna se pide.
    return not _sheet_moves_units(entity_type, mapped) and _sheet_moves_units(
        entity_type, mapped | {field}
    )


def _sheet_moves_units(entity_type: str, mapped: set[str]) -> bool:
    return SheetInventoryProfile(
        context_id="", entity=entity_type, mapped_fields=frozenset(mapped)
    ).moves_units


# ── Heurísticas: entity_type → target_field → keywords (substring match) ─────
_HEURISTICS: dict[str, dict[str, set[str]]] = {
    "sale": {
        "amount": {
            "precio_venta",
            "venta",
            "ventas",
            "ingreso",
            "monto",
            "importe",
            "total_venta",
            "total_cobrado",
            "cobro",
            "total",
            "valor",
        },
        # `mes` NO está: un mes es un período y no dice qué día (ver el concepto
        # `mes` en `header_semantics`). Como keyword seguía afirmando lo
        # contrario en las dos capas de abajo — fuzzy le pondría el campo a un
        # «Meses», y el aviso de requerido-sin-cubrir señalaría la columna «Mes»
        # como la candidata a ser la fecha, empujando al usuario justo al mapeo
        # que el reconocedor acaba de declarar indemostrable.
        "transaction_date": {"fecha", "date", "dia", "periodo"},
        "quantity": {"cantidad", "qty", "unidades", "cant", "items", "unidad"},
        # Precio REALMENTE vendido en esta fila (≠ `amount`, que es el total de la
        # venta, y ≠ `Product.sale_price_ars`, que es el vigente configurado).
        "unit_price": {
            "precio_unitario",
            "p_unitario",
            "precio_vendido",
            "precio_unidad",
            "unitario",
        },
        "payment_method": {"metodo", "medio", "pago", "forma_pago", "payment"},
        "product_name": {
            "producto",
            "descripcion",
            "nombre",
            "articulo",
            "item",
            "name",
            "concepto",
            "detalle",
        },
        "notes": {"notas", "observaciones", "obs", "comentarios", "nota", "memo"},
        # F7a: referencia al cliente (aditivo). Bare "cliente" es seguro acá — no
        # colisiona con ningún keyword existente de sale (ver product_name arriba,
        # que usa "nombre" pero no "cliente").
        "customer_dni": {"dni_cliente", "cliente_dni", "dni"},
        "customer_cuit": {"cuit_cliente", "cliente_cuit", "cuit"},
        "customer_email": {"email_cliente", "cliente_email", "email", "correo", "mail"},
        "customer_phone": {
            "telefono_cliente", "cliente_telefono", "telefono", "teléfono", "whatsapp_cliente",
        },
        "customer_name": {"cliente", "nombre_cliente", "cliente_nombre"},
    },
    "expense": {
        "amount": {
            "costo",
            "gasto",
            "gastos",
            "egreso",
            "compra",
            "pago",
            "monto",
            "importe",
            "total",
            "valor",
        },
        # Sin `mes`, por el mismo motivo que en `sale`.
        "expense_date": {"fecha", "date", "dia", "periodo"},
        "category": {"categoria", "tipo", "rubro", "clasificacion", "concepto"},
        "payment_method": {
            "forma_pago",
            "forma_de_pago",
            "metodo_pago",
            "metodo_de_pago",
            "medio_pago",
            "medio_de_pago",
            "tipo_pago",
            "payment",
        },
        "is_recurring": {"recurrente", "recurring", "es_fijo", "frecuencia"},
        "supplier_name": {
            "proveedor",
            "proveedor_nombre",
            "empresa",
            "nombre_proveedor",
            "supplier",
        },
        "notes": {"notas", "observaciones", "descripcion", "detalle", "obs"},
        # F-H6.a: los alias tienen que ser INEQUÍVOCOS dentro de `expense`. Un
        # keyword que empata en longitud con otro de esta misma entidad lo decide
        # el orden del dict — que es el incidente ASTERIA, donde "precio" y
        # "compra" empataban sobre `precio_de_compra` y ganaba el costo como
        # precio de venta. Los largos SÍ ganan (`_match_key` colapsa las
        # preposiciones y desempata por longitud): "precio_compra" (13) le gana a
        # "compra" (6) de `amount`, que es lo que hace que un libro de compras
        # deje de leer el precio unitario como el total de la línea.
        "unit_price": {
            "precio_unitario",
            "precio_unit",
            "p_unitario",
            "unitario",
            "precio_compra",
            "precio_costo",
            "costo_unitario",
            "p_costo",
        },
        "quantity": {"cantidad", "qty", "unidades", "cant", "items", "unidad"},
        # F-H6.b. "numero" y "comprobante" a secas quedan fuera: en un libro de
        # compras "número" puede ser el de orden de la fila.
        "invoice_number": {
            "numero_comprobante",
            "nro_comprobante",
            "comprobante_numero",
            "numero_factura",
            "nro_factura",
            "factura_numero",
            "n_factura",
            "remito",
            "nro_remito",
            "numero_remito",
        },
        "shipping_cost": {"envio", "flete", "shipping", "costo_envio", "gastos_envio"},
        # Deliberadamente SIN "descripcion", "detalle", "concepto" ni "nombre":
        # los tres primeros ya son de `notes`/`category` con la misma longitud
        # (empate → orden del dict), y un "nombre" suelto en una planilla de
        # compras es tan probable que sea el del proveedor. Sugerir mal es peor
        # que no sugerir: el usuario mapea a mano y sigue.
        "product_name": {"producto", "articulo", "mercaderia", "item"},
        # Igual criterio: "codigo" a secas en una compra suele ser el número de
        # comprobante, no el SKU.
        "sku": {"sku", "codigo_producto", "cod_producto"},
        "barcode": {
            "barcode",
            "ean",
            "upc",
            "gtin",
            "barras",
            "codigo_de_barras",
            "cod_barra",
            "codigo_barra",
        },
        # F7a: referencia al proveedor (aditivo). "supplier_name" ya existía arriba
        # (no se duplica); acá solo se suman los campos que faltaban.
        "supplier_cuil": {"cuil_proveedor", "proveedor_cuil", "cuil"},
        "supplier_email": {"email_proveedor", "proveedor_email", "email", "correo", "mail"},
        "supplier_phone": {
            "telefono_proveedor", "proveedor_telefono", "telefono", "teléfono",
        },
    },
    # F7a: maestro de CLIENTES (identidad fiscal/contacto — sin datos transaccionales).
    "customer": {
        "customer_type": {"tipo_cliente", "persona_empresa", "tipo"},
        "name": {"nombre", "cliente", "razon_social", "razón_social"},
        "last_name": {"apellido"},
        "doc_type": {"tipo_documento", "tipo_doc"},
        "dni": {"dni"},
        "cuit": {"cuit"},
        "iva_condition": {"condicion_iva", "condición_iva", "situacion_iva", "iva"},
        "email": {"email", "correo", "mail"},
        "phone": {"telefono", "teléfono", "celular", "whatsapp"},
        "address": {"direccion", "dirección", "domicilio"},
        "locality": {"localidad", "ciudad"},
        "province": {"provincia"},
        "postal_code": {"codigo_postal", "código_postal", "cp"},
        "birthday": {"cumpleanos", "cumpleaños", "fecha_nacimiento", "nacimiento"},
        "notes": {"notas", "observaciones", "obs", "comentarios"},
    },
    # F7a: maestro de PROVEEDORES — acotado a los campos que persiste el modelo
    # Supplier hoy (ver CANONICAL_FIELDS["supplier"] arriba).
    "supplier": {
        "name": {"nombre", "proveedor", "razon_social", "razón_social"},
        "last_name": {"apellido"},
        "cuil": {"cuil"},
        "cuit": {"cuit"},
        "iva_condition": {"condicion_iva", "condición_iva", "situacion_iva", "iva"},
        "payment_method": {
            "forma_pago", "forma_de_pago", "medio_pago", "condicion_pago", "payment",
        },
        "email": {"email", "correo", "mail"},
        # «contacto» NO está acá: no nombra un teléfono sino a la persona con la
        # que se habla, y con «Contacto» y «Teléfono» en la misma hoja el fuzzy
        # lo matcheaba con ratio 1.0 y el nombre terminaba en el teléfono del
        # proveedor.
        #
        # Sacarlo es defensa en profundidad, NO la pieza que sostiene el fix: lo
        # que corta de verdad es el concepto `contacto` de `header_semantics` con
        # su `duda`, porque `suggest_mappings` saltea el fuzzy en cuanto la
        # lectura trae una duda. Comprobado mutando: devolver este keyword solo
        # deja la suite verde; hay que sacar además el concepto para que se
        # ponga roja.
        "phone": {"telefono", "teléfono", "celular", "whatsapp"},
        "notes": {"notas", "observaciones", "obs", "comentarios"},
    },
    "product": {
        "sku": {"sku", "codigo", "código", "code", "ref", "id_producto"},
        # Tokens distintivos de código de barras. "codigo_de_barras" (más largo)
        # le gana a "codigo" de sku en el desempate por longitud de _heuristic_match.
        "barcode": {
            "barcode", "ean", "upc", "gtin", "barras",
            "codigo_de_barras", "cod_barra", "codigo_barra",
        },
        "name": {
            "producto",
            "descripcion",
            "descripción",
            "nombre",
            "articulo",
            "artículo",
            "item",
            "name",
            "concepto",
            "detalle",
        },
        # Los tres precios de un catálogo son campos DISTINTOS. Los keywords
        # largos y específicos ("precio_compra", "precio_lista",
        # "precio_venta_final") le ganan al genérico "precio" gracias a
        # `_match_key`, que colapsa las preposiciones antes de comparar.
        "sale_price_ars": {
            "precio_venta",
            "precio",
            "price",
            "p_venta",
            "venta",
            "precio_venta_final",
            "venta_final",
            "precio_final",
        },
        "list_price_ars": {
            "lista",
            "precio_lista",
            "sugerido",
            "precio_sugerido",
            "precio_venta_sugerido",
            "pvp",
        },
        # "precio unitario" en un CATÁLOGO es el costo al que se compra la unidad
        # (no el precio al que se vende: ese es `sale_price_ars`). En una hoja de
        # VENTAS el mismo header significa lo vendido y va a `sale.unit_price`.
        "unit_cost_ars": {
            "costo",
            "cost",
            "precio_costo",
            "p_costo",
            "costo_unitario",
            "compra",
            "precio_compra",
            "costo_compra",
            "precio_unitario",
            "p_unitario",
            "unitario",
        },
        "stock_units": {
            "stock",
            "cantidad",
            "inventario",
            "units",
            "qty",
            "existencia",
            "unidades",
        },
        "category": {"categoria", "tipo", "rubro"},
        "description": {"descripcion", "descripción", "detalle", "comentarios"},
        # F6-B1: fechas de producto. La palabra genérica "fecha" NO auto-mapea
        # ninguna (evita robarle la columna de fecha de venta/gasto en hojas mixtas).
        "acquired_at": {
            "alta", "adquisicion", "adquisición",
            "fecha_alta", "fecha_ingreso", "fecha_compra",
        },
        "expiry_date": {
            "vencimiento", "caducidad", "vence", "vto",
            "expira", "expiracion", "expiración",
        },
    },
}


def _normalize_col(col: str) -> str:
    """Normalizar header para matching: lowercase + underscore.

    NO tocar sin migrar datos: este es el valor que se persiste en
    ``tenant_column_mappings.source_column`` (el historial de alias aprendidos por
    cada tenant). Cambiar la forma normalizada dejaría huérfano todo lo aprendido.
    Para ajustar el matching heurístico está ``_match_key``, que deriva de acá y
    NO se persiste.

    En particular NO saca acentos: por eso conserva la tilde que trajo el archivo
    que enseñó el alias. La tolerancia al acento se resuelve al LEER el historial
    (índice plegado por ``fold_header`` en ``suggest_mappings``), no al escribirlo.
    """
    return col.lower().strip().replace(" ", "_").replace("-", "_")


def _match_key(normalized: str) -> str:
    """Clave de matching heurístico: el header normalizado sin preposiciones ni
    acentos.

    Existe por un empate real. ``_heuristic_match`` gana con el keyword MÁS LARGO
    y solo reemplaza si es estrictamente mayor, así que sobre ``precio_de_compra``
    los keywords ``precio`` (6, ``sale_price_ars``) y ``compra`` (6,
    ``unit_cost_ars``) empataban y ganaba el primero que se iterara — el costo de
    compra entraba como precio de venta (incidente ASTERIA). Con la clave
    ``precio_compra`` el keyword ``precio_compra`` (13) le gana a ``precio`` (6) y
    el desempate deja de depender del orden de un dict.

    También pliega acentos y ñ (``Descripción`` ≡ ``Descripcion``, ``Año`` ≡
    ``Ano``), incluyendo NFC vs NFD. Por eso este es el único lugar donde los
    acentos se pueden sacar: ``_normalize_col`` no puede, porque es lo que se
    persiste, así que la tolerancia tiene que vivir en la clave derivada.

    Deliberadamente NO se toca ``_normalize_col``: esa alimenta el historial
    persistido por tenant.

    La implementación vive en ``app.domain.header_keys`` porque el clasificador de
    hojas la necesita para lo mismo; acá queda el alias para no tocar los call
    sites ni el razonamiento de arriba.
    """
    return match_key(normalized)


# Los mismos keywords ya pasados por `_match_key`, precomputados al importar: el
# matching compara clave contra clave, así un keyword escrito "forma_de_pago"
# sigue matcheando un header "forma pago" sin tener que declarar las dos formas.
_HEURISTIC_KEYS: dict[str, dict[str, frozenset[str]]] = {
    entity: {
        target: frozenset(_match_key(k) for k in keywords) for target, keywords in targets.items()
    }
    for entity, targets in _HEURISTICS.items()
}


# ── F-M: de (entidad, concepto, calificadores) al campo ──────────────────────
#
# La segunda mitad del reconocedor. `header_semantics` dice QUÉ es la columna en
# castellano; esta tabla dice a qué campo va en ESTA entidad — y, cuando no
# alcanza para decidir, cuáles son los candidatos y por qué no alcanza.
#
# Vive acá y no en el dominio porque nombra `target_field`s: el vocabulario del
# idioma no tiene por qué conocer el catálogo de campos de la base.


@dataclass(frozen=True)
class ReglaDeTarget:
    """Qué hacer con un concepto cuando aparecen ciertos calificadores.

    Las reglas de un concepto se evalúan EN ORDEN y gana la primera cuyos
    calificadores estén todos presentes. La última suele tener ``si`` vacío: es
    el caso "sin nada que desempate".
    """

    si: frozenset[str] = frozenset()
    #: Resultado inequívoco.
    target: str | None = None
    #: Dos o más candidatos: se le pregunta al usuario.
    opciones: tuple[str, ...] = ()
    #: Por qué no alcanza, en castellano. Obligatoria si no hay `target`.
    duda: str | None = None


def _r(*args: str, **kw: object) -> ReglaDeTarget:
    """Azúcar: `_r("unitario", target="unit_price")`."""
    return ReglaDeTarget(si=frozenset(args), **kw)  # type: ignore[arg-type]


_PRECIO_LINEA_O_UNIDAD = "¿es el precio de cada unidad, o el total de la línea?"
_SIN_CAMPO_DESCUENTO = (
    "Es un descuento. Véktor todavía no tiene un campo propio para descuentos de "
    "una compra; se puede guardar como campo propio."
)
_SIN_CAMPO_IMPUESTO = (
    "Es un impuesto de la línea. Véktor todavía no tiene un campo propio para eso; "
    "se puede guardar como campo propio."
)
_ENVIO_UNITARIO = (
    "Es un costo de envío por unidad. Véktor sabe leer el envío del comprobante y "
    "el que ya viene asignado a cada línea, pero no el de cada unidad."
)
_ENVIO_POR_LINEA = (
    "Es el envío que le toca a esta línea, no el del comprobante entero. Los dos "
    "se leen con reglas opuestas —uno se cobra una vez, el otro se suma— así que "
    "no se pueden usar como si fueran el mismo campo."
)
_MONTO_DEL_COMPROBANTE = (
    "Parece el total del comprobante, no el de esta línea. Importarlo como el monto "
    "de la fila repetiría el total en cada línea del remito."
)
#: Un mes es un período: no dice el día, y el día no se completa solo. Mapearlo
#: al campo de fecha además le disputaba el campo a la columna de fecha real de
#: la hoja —«Mes» y «Fecha de Pago» conviven en la misma planilla de gastos fijos
#: y las dos son escalares, así que el confirm cortaba con un 422— y el usuario
#: terminaba mandando `Mes` a un campo propio a mano. Se reconoce, se explica, y
#: si de verdad trae fechas la elige la persona: es la única que puede saberlo.
_MES_NO_DICE_EL_DIA = (
    "Es un mes, que es un período y no una fecha: «Marzo» no dice qué día. Véktor "
    "no completa el día que falta, así que no la toma como la fecha de la hoja. Si "
    "la columna en realidad trae fechas, elegí el campo de fecha a mano; si no, se "
    "guarda como campo propio."
)
#: `transaction_date`/`expense_date` son DATETIME (migración `20260625_0001`)
#: justamente para soportar intradía, así que la hora TIENE dónde vivir — lo que
#: todavía no existe es el paso que combina una columna de hora con una de fecha,
#: y eso es del importador. Prometerlo acá sería mentir; callarlo dejaba el
#: encabezado sin ninguna lectura (y a «Hora de venta» entrando como monto).
_HORA_NO_SE_COMBINA_CON_LA_FECHA = (
    "Es la hora del movimiento. Véktor guarda la fecha con hora, pero todavía no "
    "combina una columna de hora con una de fecha: se guarda como campo propio, "
    "aparte de la fecha."
)
#: El margen NO tiene campo canónico, y no es un campo que falte: es una decisión.
#: Sale de restar el costo al precio de venta, así que importarlo además como dato
#: deja dos números para la misma métrica y ninguna regla para saber cuál gana —
#: exactamente lo que el invariante de una sola fuente por métrica viene a evitar
#: (``FactsService``). Se reconoce y se explica; el valor se conserva como campo
#: propio, que es lo mismo que se hace con la marca.
_MARGEN_ES_DERIVADO = (
    "Es el margen de ganancia. Véktor lo calcula desde el costo y el precio de "
    "venta: si además se importa como dato, quedan dos números para lo mismo que "
    "pueden no coincidir (redondeo, un valor viejo, o un porcentaje calculado "
    "sobre el costo y no sobre el precio). Se guarda como campo propio."
)

#: «Contacto» en un padrón de proveedores/clientes. No se resuelve a `phone`
#: aunque a veces traiga un número: en las planillas reales esa columna trae
#: tanto el nombre de la persona como su teléfono, y el encabezado no distingue.
#: Adivinar acá no es gratis — con «Contacto» y «Teléfono» juntos, el nombre
#: pisaba el teléfono del proveedor por orden de columna.
_CONTACTO_NO_DICE_QUE_DATO_ES = (
    "Es el contacto del proveedor, pero el encabezado no dice qué dato es: puede "
    "ser el nombre de la persona con la que se habla o su teléfono. Véktor no "
    "tiene campo de persona de contacto, así que se guarda como campo propio; si "
    "la columna trae el número, mapeala a mano a Teléfono."
)

RESOLUCION: dict[str, dict[str, tuple[ReglaDeTarget, ...]]] = {
    "sale": {
        "fecha": (_r(target="transaction_date"),),
        "mes": (_r(duda=_MES_NO_DICE_EL_DIA),),
        "hora": (_r(duda=_HORA_NO_SE_COMBINA_CON_LA_FECHA),),
        "monto": (
            _r("por_comprobante", duda=_MONTO_DEL_COMPROBANTE),
            _r(
                "de_pago",
                opciones=("amount", "payment_method"),
                duda="¿es cuánto se pagó, o con qué se pagó?",
            ),
            _r(target="amount"),
        ),
        "precio": (
            _r("unitario", target="unit_price"),
            _r(opciones=("unit_price", "amount"), duda=_PRECIO_LINEA_O_UNIDAD),
        ),
        "cantidad": (_r(target="quantity"),),
        "producto": (_r(target="product_name"),),
        "cliente": (_r(target="customer_name"),),
        # `Nombre` a secas en una hoja de ventas ya era el del producto: es una
        # decisión previa del proyecto, no un empate de longitud, así que se
        # conserva.
        "nombre": (
            _r("de_cliente", target="customer_name"),
            _r(target="product_name"),
        ),
        "dni": (_r(target="customer_dni"),),
        "cuit": (_r(target="customer_cuit"),),
        "email": (_r(target="customer_email"),),
        "telefono": (_r(target="customer_phone"),),
        "metodo_pago": (_r(target="payment_method"),),
        "nota": (_r(target="notes"),),
        "descripcion": (_r(target="product_name"),),
        "margen": (_r(duda=_MARGEN_ES_DERIVADO),),
    },
    "expense": {
        "fecha": (_r(target="expense_date"),),
        "mes": (_r(duda=_MES_NO_DICE_EL_DIA),),
        "hora": (_r(duda=_HORA_NO_SE_COMBINA_CON_LA_FECHA),),
        "monto": (
            _r("por_comprobante", duda=_MONTO_DEL_COMPROBANTE),
            _r(target="amount"),
        ),
        "precio": (
            _r("unitario", target="unit_price"),
            _r("de_compra", target="unit_price"),
            _r(opciones=("unit_price", "amount"), duda=_PRECIO_LINEA_O_UNIDAD),
        ),
        "costo": (
            _r("unitario", target="unit_price"),
            # «Precio costo» es el costo POR UNIDAD, no el total de la línea: es
            # como una planilla de compras nombra el costo unitario.
            _r("de_precio", target="unit_price"),
            _r("de_producto", opciones=("unit_price", "amount"), duda=_PRECIO_LINEA_O_UNIDAD),
            _r("final", opciones=("unit_price", "amount"), duda=_PRECIO_LINEA_O_UNIDAD),
            _r(target="amount"),
        ),
        "cantidad": (_r(target="quantity"),),
        "categoria": (_r(target="category"),),
        "metodo_pago": (_r(target="payment_method"),),
        "recurrencia": (_r(target="is_recurring"),),
        "proveedor": (_r(target="supplier_name"),),
        "producto": (_r(target="product_name"),),
        "nombre": (
            _r("de_proveedor", target="supplier_name"),
            _r("de_producto", target="product_name"),
            _r(
                opciones=("product_name", "supplier_name"),
                duda="¿es el nombre del producto de la línea, o el del proveedor?",
            ),
        ),
        "sku": (_r(target="sku"),),
        "codigo": (_r(target="sku"),),
        "barcode": (_r(target="barcode"),),
        "comprobante": (_r(target="invoice_number"),),
        "envio": (
            # Sigue sin haber campo para el flete POR UNIDAD: Véktor lee el del
            # comprobante y el ya asignado a la línea, no una tercera granularidad.
            _r("unitario", duda=_ENVIO_UNITARIO),
            _r("por_linea", target="shipping_cost_line"),
            # `Envío` a secas resuelve al del comprobante y NO se vuelve ambiguo:
            # F-H6.b ya le pregunta al usuario la granularidad (`una_por_hoja` vs
            # `una_por_fila`) cuando la hoja no trae comprobante, y esa pregunta se
            # hace donde el número está a la vista. Preguntarlo dos veces es
            # fricción en el encabezado más común de un remito. Límite declarado:
            # un archivo con flete por línea Y comprobante entra como si fuera del
            # comprobante, salvo que el usuario mapee la columna a mano.
            _r(target="shipping_cost"),
        ),
        "descuento": (_r(target="discount"),),
        "impuesto": (_r(target="taxes"),),
        "cuil": (_r(target="supplier_cuil"),),
        "email": (_r(target="supplier_email"),),
        "telefono": (_r(target="supplier_phone"),),
        "nota": (_r(target="notes"),),
        "descripcion": (_r(target="notes"),),
        "margen": (_r(duda=_MARGEN_ES_DERIVADO),),
    },
    "product": {
        "precio": (
            _r("de_lista", target="list_price_ars"),
            _r("de_compra", target="unit_cost_ars"),
            _r("de_venta", target="sale_price_ars"),
            _r("unitario", target="unit_cost_ars"),
            _r("final", target="sale_price_ars"),
            _r(
                opciones=("sale_price_ars", "unit_cost_ars", "list_price_ars"),
                duda=(
                    "Los tres precios de un producto son campos distintos y "
                    "conviven: el de venta, el costo y el de lista. El encabezado "
                    "no dice cuál es."
                ),
            ),
        ),
        "costo": (_r(target="unit_cost_ars"),),
        # «Compra» y «Venta» a secas, en un catálogo, dicen cuál de los tres
        # precios es la columna. Sin calificador NO hay regla: un «Monto» pelado
        # en un catálogo no dice cuál de los tres es, y adivinarlo es el bug que
        # F10 cerró.
        "monto": (
            _r("de_compra", target="unit_cost_ars"),
            _r("de_venta", target="sale_price_ars"),
        ),
        "nombre": (_r(target="name"),),
        "producto": (_r(target="name"),),
        "sku": (_r(target="sku"),),
        "codigo": (_r(target="sku"),),
        "barcode": (_r(target="barcode"),),
        "stock": (_r(target="stock_units"),),
        "cantidad": (_r(target="stock_units"),),
        "categoria": (_r(target="category"),),
        "descripcion": (_r(target="description"),),
        "vencimiento": (_r(target="expiry_date"),),
        "fecha": (_r(target="acquired_at"),),
        # Sin la entrada explícita, el genérico dice «esta hoja no tiene un campo
        # para eso», que acá es falso: el catálogo TIENE `acquired_at`. Lo que no
        # se puede es derivarlo de un mes ni de una hora sueltos.
        "mes": (_r(duda=_MES_NO_DICE_EL_DIA),),
        "hora": (_r(duda=_HORA_NO_SE_COMBINA_CON_LA_FECHA),),
        "marca": (
            _r(
                duda=(
                    "Es la marca del producto. No es un campo del catálogo —una "
                    "marca no es un proveedor, ver la Reforma de Proveedores—: se "
                    "guarda como campo propio."
                )
            ),
        ),
        "margen": (_r(duda=_MARGEN_ES_DERIVADO),),
    },
    "customer": {
        # Una columna «Cliente» en un padrón de clientes ES el nombre; «Tipo
        # cliente» es su clasificación. Sin estas dos reglas el encabezado más
        # canónico del import de clientes no mapeaba, y `name` es requerido.
        "cliente": (
            _r("clasificador", target="customer_type"),
            _r(target="name"),
        ),
        # «IVA», «Condición IVA», «Situación IVA»: acá el impuesto no es un monto,
        # es la categoría fiscal de la persona.
        "impuesto": (_r(target="iva_condition"),),
        "nombre": (_r(target="name"),),
        "apellido": (_r(target="last_name"),),
        "dni": (_r(target="dni"),),
        "cuit": (_r(target="cuit"),),
        "email": (_r(target="email"),),
        "telefono": (_r(target="phone"),),
        "direccion": (_r(target="address"),),
        "localidad": (_r(target="locality"),),
        "provincia": (_r(target="province"),),
        "codigo_postal": (_r(target="postal_code"),),
        "cumpleanos": (_r(target="birthday"),),
        "nota": (_r(target="notes"),),
    },
    "supplier": {
        # Espejo de `cliente` en customer: «Proveedor» en un padrón de
        # proveedores es el nombre, y `name` es requerido.
        "proveedor": (_r(target="name"),),
        "nombre": (_r(target="name"),),
        "apellido": (_r(target="last_name"),),
        "cuil": (_r(target="cuil"),),
        # Espejo de customer: el CUIT es un identificador propio, y «IVA» /
        # «Condición IVA» acá no nombran un monto de impuesto sino la condición
        # frente a AFIP.
        "cuit": (_r(target="cuit"),),
        "impuesto": (_r(target="iva_condition"),),
        "email": (_r(target="email"),),
        "telefono": (_r(target="phone"),),
        "metodo_pago": (_r(target="payment_method"),),
        "nota": (_r(target="notes"),),
        "contacto": (_r(duda=_CONTACTO_NO_DICE_QUE_DATO_ES),),
    },
}


@dataclass(frozen=True)
class HeaderReading:
    """Los tres resultados posibles de leer un encabezado.

    - ``unico``: se puede demostrar qué campo es. Se propone.
    - ``ambiguo``: hay más de una lectura razonable. Se ofrecen y se explica.
    - ``sin_evidencia``: no alcanza. La columna se conserva y se pregunta.

    Los dos últimos son lo mismo para el importador —la columna no se mapea
    sola— y distintos para la persona: «no entiendo esto» y «entiendo qué es
    pero no tengo dónde ponerlo» no se explican igual.
    """

    outcome: Literal["unico", "ambiguo", "sin_evidencia"]
    target: str | None = None
    options: tuple[str, ...] = ()
    duda: str | None = None
    concept: str | None = None
    #: Lo que el encabezado decía ADEMÁS del concepto. No cambia la decisión —esa
    #: ya la tomó la regla— pero es lo único que permite explicarla: sin esto,
    #: «Envío unitario» y «Envío» son el mismo mensaje en pantalla.
    qualifiers: frozenset[str] = frozenset()


def read_header(normalized: str, entity_type: str) -> HeaderReading:
    """Lee un encabezado y devuelve uno de los tres resultados.

    Nunca elige entre dos lecturas razonables: si Véktor no puede demostrar qué
    quiso decir el usuario, conserva el dato y pregunta. Transformarlo en silencio
    en otro concepto contable es lo que convertía un flete en un precio de compra.
    """
    analisis = analyze_header(normalized)
    quals = analisis.qualifiers
    if analisis.concept is None:
        if analisis.rivals:
            return HeaderReading(
                "sin_evidencia",
                duda=(
                    "El encabezado nombra dos cosas a la vez y ninguna manda sobre "
                    "la otra."
                ),
                qualifiers=quals,
            )
        return HeaderReading("sin_evidencia", qualifiers=quals)

    sin_campo = HeaderReading(
        "sin_evidencia",
        duda=f"Esta hoja no tiene un campo para eso ({analisis.concept}).",
        concept=analisis.concept,
        qualifiers=quals,
    )
    reglas = RESOLUCION.get(entity_type, {}).get(analisis.concept)
    if reglas is None:
        return sin_campo

    for regla in reglas:
        if regla.si <= quals:
            if regla.target is not None:
                return HeaderReading(
                    "unico",
                    target=regla.target,
                    concept=analisis.concept,
                    qualifiers=quals,
                )
            if regla.opciones:
                return HeaderReading(
                    "ambiguo",
                    options=regla.opciones,
                    duda=regla.duda,
                    concept=analisis.concept,
                    qualifiers=quals,
                )
            return HeaderReading(
                "sin_evidencia",
                duda=regla.duda,
                concept=analisis.concept,
                qualifiers=quals,
            )
    return sin_campo


def heuristic_target(
    normalized: str, entity_type: str, *, prefer: tuple[str, ...] = ()
) -> str | None:
    """El target de un encabezado, o ``None`` si no se puede demostrar cuál es.

    Para los consumidores SINCRÓNICOS —la extracción de remitos y la de
    proveedores— que no tienen pantalla donde desambiguar.

    ``prefer`` es cómo el llamador aporta el contexto que a esta función le falta.
    Una ambigüedad puede ser real en general y estar resuelta por el TIPO DE
    DOCUMENTO: «Precio» en un catálogo no dice cuál de los tres es, pero en un
    remito —que es un documento de líneas, no un catálogo— es el precio de esa
    línea. El que sabe eso es quien lee el remito, no el reconocedor, así que lo
    declara en vez de que nadie lo decida.

    Sin ``prefer`` un `ambiguo` es lo mismo que un desconocido: no hay a quién
    preguntarle. Y eso NO es gratis — se midió: un remito con columnas
    «Producto | Cantidad | Precio | Total | Código» se quedaba sin ninguna columna
    de precio. Un llamador sin pantalla que además no declare su preferencia
    pierde el dato en silencio.
    """
    lectura = read_header(normalized, entity_type)
    if lectura.outcome == "unico":
        return lectura.target
    if lectura.outcome == "ambiguo" and prefer:
        for candidato in prefer:
            if candidato in lectura.options:
                return candidato
    return None


# ── Campos de valor único ────────────────────────────────────────────────────
# Un campo escalar solo puede venir de UNA columna. Si dos apuntan al mismo, el
# importador se quedaba con la primera del orden del archivo y descartaba el
# resto en silencio (`_resolve_target_cols`): elegir un dato de negocio por un
# detalle de implementación es inventarlo. El confirm ahora lo rechaza y la UI lo
# bloquea, las dos leyendo de acá.
#
# Alcance deliberado: montos, cantidades, fechas y los tres precios — donde una
# colisión corrompe plata. `name`/`notes`/`category` quedan afuera (varias
# columnas pueden ser legítimas) y se cubren con un aviso no bloqueante.
# Campos de una venta/gasto que REFERENCIAN a un cliente o proveedor. Mapear
# alguno hace que el import cree o toque ese maestro como efecto lateral, aunque
# el archivo no traiga una hoja de maestros. El borrado necesita saberlo para
# poder revertirlos (ver `_trae_maestros` en el confirm).
MASTER_REFERENCE_TARGETS: frozenset[str] = frozenset(
    {
        "customer_name",
        "customer_dni",
        "customer_cuit",
        "customer_email",
        "customer_phone",
        "supplier_name",
        "supplier_cuil",
        "supplier_email",
        "supplier_phone",
    }
)

SINGLE_VALUE_FIELDS: dict[str, frozenset[str]] = {
    "sale": frozenset({"amount", "quantity", "transaction_date", "unit_price"}),
    # F-H6.a: los nuevos son escalares por la misma razón que en `sale` — dos
    # columnas al mismo destino no se pueden desempatar sin inventar, y hasta F-0
    # `_resolve_target_cols` se quedaba con la primera del orden del Excel.
    "expense": frozenset(
        {
            "amount",
            "expense_date",
            "quantity",
            "unit_price",
            # F-M.7: escalares por el mismo motivo. Dos columnas de descuento
            # sobre la misma línea no se suman solas ni se elige una.
            "shipping_cost_line",
            "discount",
            "taxes",
        }
    ),
    "product": frozenset(
        {"sale_price_ars", "list_price_ars", "unit_cost_ars", "stock_units"}
    ),
    # Los maestros quedaron sin ningún campo escalar hasta acá, y no porque sus
    # campos admitan varias columnas: un proveedor tiene UN CUIL y UN teléfono
    # igual que una venta tiene UN monto. La guarda se había pensado para "no
    # corromper plata", y una identidad no es plata — pero se pisa igual y se
    # descubre peor: un monto equivocado salta en un total, un teléfono
    # equivocado no salta en ningún lado.
    #
    # El caso que lo destapó: una hoja de proveedores con «Contacto» (col 6) y
    # «Teléfono» (col 7). Las dos resuelven a `phone` —`contacto` es keyword de
    # `phone` a propósito— y `_resolve_target_cols` es first-wins por orden de
    # columna, así que el teléfono del proveedor quedaba siendo el NOMBRE de la
    # persona de contacto. Ahora se le pregunta al usuario cuál es cuál, y la
    # otra columna puede ir a un campo propio en vez de perderse.
    #
    # `notes` queda AFUERA, igual que en `sale`/`expense`/`product`: es texto
    # libre, no un dato de identidad, y es el único de estos campos donde tener
    # dos columnas («Observaciones» y «Comentarios») es una forma razonable de
    # llenar una ficha y no un empate que haya que desempatar.
    "customer": frozenset(
        {
            "customer_type",
            "name",
            "last_name",
            "doc_type",
            "dni",
            "cuit",
            "iva_condition",
            "email",
            "phone",
            "address",
            "locality",
            "province",
            "postal_code",
            "birthday",
        }
    ),
    "supplier": frozenset(
        {"name", "last_name", "cuil", "cuit", "iva_condition", "payment_method",
         "email", "phone"}
    ),
}


# ── F-0: gramática de un ``target_field`` ────────────────────────────────────
# Un target puede ser cuatro cosas y hasta acá cada consumidor las distinguía
# con su propio ``startswith("custom_field:")``. Seis copias de la misma regla
# son seis oportunidades de que una quede vieja — y la próxima forma de target
# (``{entidad}:{campo}``, ruteo entre secciones) usa el MISMO separador que los
# campos propios, así que una copia desactualizada empezaría a leer
# ``custom_field:marca`` como "entidad custom_field, campo marca".

#: Entidades que pueden aparecer como prefijo de un target cruzado. Es lo que
#: distingue ``customer:dni`` (cruzado) de ``custom_field:marca`` (campo propio)
#: sin depender del orden en que aparezcan los dos puntos.
CROSS_ENTITY_PREFIXES: frozenset[str] = frozenset(CANONICAL_FIELDS)

_CUSTOM_FIELD_PREFIX = "custom_field:"


@dataclass(frozen=True)
class ParsedTarget:
    """Qué es un ``target_field``, resuelto en un solo lugar.

    ``kind``:
      - ``none``      — sin mapear (nadie lo miró todavía)
      - ``ignore``    — el usuario decidió explícitamente dejarla afuera
      - ``canonical`` — campo de la entidad de la propia hoja
      - ``custom``    — campo propio del tenant; ``field`` es la clave sin prefijo
      - ``cross``     — campo de OTRA entidad; ``entity`` dice cuál

    ``none`` e ``ignore`` no se colapsan a propósito: uno es una columna sin
    revisar y el otro una decisión tomada. Tratarlos igual deja que una columna
    que nadie miró se descarte como si alguien lo hubiera querido.
    """

    kind: str
    entity: str | None
    field: str


def parse_target(target: str | None) -> ParsedTarget:
    """Fuente ÚNICA de verdad sobre qué representa un ``target_field``.

    Nadie más debería hacer ``startswith("custom_field:")`` a mano.
    """
    if target is None or not target.strip():
        return ParsedTarget(kind="none", entity=None, field="")
    value = target.strip()
    if value == "ignore":
        return ParsedTarget(kind="ignore", entity=None, field="")
    if value.startswith(_CUSTOM_FIELD_PREFIX):
        # La clave se normaliza igual que el resto del string: sin esto,
        # "custom_field:obs " y "custom_field:obs" eran la misma clave (el strip
        # de afuera la alcanzaba) pero "custom_field: obs" era OTRA, así que dos
        # columnas podían compartir campo sin que la colisión se detectara.
        return ParsedTarget(
            kind="custom", entity=None, field=value[len(_CUSTOM_FIELD_PREFIX) :].strip()
        )
    prefix, sep, rest = value.partition(":")
    if sep and prefix in CROSS_ENTITY_PREFIXES and rest:
        return ParsedTarget(kind="cross", entity=prefix, field=rest)
    # Prefijo desconocido: NO se inventa una entidad. Queda como canónico, que es
    # la forma que el confirm ya sabe rechazar cuando el campo no existe.
    return ParsedTarget(kind="canonical", entity=None, field=value)


#: Rutas de escritura entre secciones habilitadas, explícitas por par
#: (entidad de la hoja → entidad destino → campos). NO es un producto
#: cartesiano: cada par se habilita a mano porque cada uno tiene una semántica
#: distinta de identidad y de escritura.
#:
#: REGLA que gobierna qué entra (la testea ``test_ningun_cruzado_duplica_una_
#: referencia_canonica``): una ruta cruzada existe para alcanzar campos que la
#: entidad de la hoja NO puede expresar. Si el campo ya tiene contraparte
#: canónica en la hoja de origen —convención ``{entidad}_{campo}``:
#: ``customer_dni``, ``supplier_name``, ``product_name``— queda FUERA, porque
#: dos rutas para la misma columna con semánticas de creación distintas es un
#: bug esperando: la canónica pasa por el resolvedor de referencias (cuya
#: creación gobierna ``*_REFERENCE_CREATION_MODE``) y la cruzada escribiría el
#: maestro directo, sin arbitraje entre las dos.
#:
#: Fuera a propósito, además de lo que saca la regla:
#:  - ``product:stock_units`` desde cualquier hoja — es la proyección de un
#:    ledger de movimientos, no un campo que se setea desde una columna.
#:  - ``product → supplier:*`` — un catálogo de productos NO crea proveedores:
#:    la columna "Tienda"/"Proveedor" de un catálogo es la MARCA, y va a
#:    ``Product.custom_fields["marca"]``. Habilitar esta ruta recrearía
#:    exactamente las filas marca-como-proveedor que hubo que limpiar con
#:    ``deactivate_brand_suppliers.py`` + el flag ``_brand_collapsed``. Si F-D
#:    la quiere, primero tiene que definir que solo VINCULE a un proveedor
#:    existente y nunca cree.
#:  - ``notes`` — no es escalar, así que dos columnas podrían apuntarle y habría
#:    que inventar cómo concatenarlas.
CROSS_ENTITY_TARGETS: dict[str, dict[str, frozenset[str]]] = {
    "sale": {
        # name/dni/cuit/email/phone quedan fuera: ya son customer_* canónicos
        # de la venta y los consume `_customer_reference_record`.
        "customer": frozenset(
            {
                "last_name",
                "address",
                "locality",
                "province",
                "postal_code",
                "customer_type",
                "iva_condition",
            }
        ),
    },
    "expense": {
        "product": frozenset({"sku", "barcode", "unit_cost_ars", "category"}),
        # name/cuil/email/phone quedan fuera: ya son supplier_* canónicos del
        # gasto y los consume `_supplier_reference_record`.
        "supplier": frozenset({"last_name", "payment_method"}),
    },
    # Bloque 2: "Tienda" de un catálogo YA NO es SIEMPRE marca — el usuario
    # puede mapearla a `supplier:name` para declarar un proveedor real (F10
    # invariante de "Tienda"/CROSS_ENTITY_TARGETS histórico). Gateado por
    # `PRODUCT_SUPPLIER_LINKS_ROLLOUT_TENANT_IDS`; con el flag apagado, el
    # target queda inerte (ingestion_import_service lo descarta como cualquier
    # cruzado no aplicado, comportamiento idéntico al de hoy).
    "product": {
        "supplier": frozenset({"name"}),
    },
    "customer": {},
    "supplier": {},
}

#: Campos que ninguna ruta cruzada puede escribir, pase lo que pase. Es defensa
#: en profundidad sobre ``CROSS_ENTITY_TARGETS``: la allowlist ya no los tiene,
#: y este guard rechaza igual si alguien los agrega por error.
CROSS_ENTITY_FORBIDDEN_FIELDS: frozenset[str] = frozenset({"stock_units"})


# Targets canónicos que representan la fecha de negocio de una fila.
_DATE_TARGET_FIELDS: frozenset[str] = frozenset({"transaction_date", "expense_date"})


def resolve_transaction_date_column(
    headers: list[str] | None,
    mappings: dict[str, str] | None,
) -> str | None:
    """Fuente ÚNICA de verdad: ¿esta hoja/entidad tiene una columna de fecha
    resoluble? Devuelve el nombre de columna, o ``None`` si no hay ninguna.

    Precedencia idéntica a la del importador (F6-A1): primero el mapeo explícito
    (``source_col`` cuyo ``target`` sea ``transaction_date``/``expense_date``),
    luego la heurística por substring del header contra ``FECHA_COLS`` — el mismo
    criterio que ``file_parsing.has_fecha`` y que ``_find_col(headers, FECHA_COLS)``.
    Sin esta función, la API y el importador terminaban divergiendo sobre "esta
    hoja tiene fecha o no" (ver C1).
    """
    if mappings:
        for src, target in mappings.items():
            # El mapeo explícito solo vale si la columna EXISTE en la hoja. Un
            # payload viejo/inconsistente como {"col_inexistente": "transaction_date"}
            # pasaría el gate, pero el importador obtendría None por fila (la columna
            # no está) y mandaría todo a /otros — el 422-antes-del-lease se saltearía.
            if target in _DATE_TARGET_FIELDS and (headers is None or src in headers):
                return src
    if headers:
        # Import local: file_parsing es un módulo pesado; column_mapping_service se
        # importa desde routers livianos. FECHA_COLS es el set canónico de keywords.
        from app.application.services.file_parsing import FECHA_COLS  # noqa: PLC0415

        for h in headers:
            norm = _normalize_col(h)
            if any(k in norm for k in FECHA_COLS):
                return h
    return None


def validate_required_date_mapping(
    included: list[tuple[str, list[str] | None, dict[str, str]]],
) -> list[str]:
    """F6-A1: dado un conjunto de contextos venta/gasto INCLUIDOS en el import,
    devuelve las etiquetas de los que NO tienen columna de fecha resoluble.

    Cada elemento de ``included`` es ``(label, headers, mappings)``. La API arma
    la lista desde su estado de confirmación (flat vs por-contexto) y esta función
    aplica el mismo resolver que el importador — sin que el router toque
    ``FECHA_COLS`` ni ``_find_col`` (ambos privados del pipeline de import).

    Lista vacía = todos los contextos incluidos tienen fecha.
    """
    missing: list[str] = []
    for label, headers, mappings in included:
        if resolve_transaction_date_column(headers, mappings) is None:
            missing.append(label)
    return missing


def _heuristic_match(normalized: str, entity_type: str) -> str | None:
    """Busca el target_field para un header normalizado.

    1. Match exacto (gana siempre), contra el header crudo y contra su
       ``_match_key`` — así "Precio de compra" y "Precio compra" resuelven igual
       sin duplicar cada keyword.
    2. Substring sobre la clave: gana el keyword MÁS LARGO entre todos los campos
       — evita que un keyword corto y genérico de otro campo capture un header
       específico (ej: `forma_pago` debe ir a payment_method por "forma_pago", no
       a amount por el substring "pago").

    Ante un empate de longitud gana el primero declarado en ``_HEURISTICS``. Eso
    ya no puede corromper un campo escalar en silencio: la colisión se valida
    aguas arriba (``SINGLE_VALUE_FIELDS``) y el confirm la rechaza.
    """
    heuristics = _HEURISTICS.get(entity_type, {})
    keyed = _HEURISTIC_KEYS.get(entity_type, {})
    key = _match_key(normalized)
    for target_field, keywords in heuristics.items():
        if normalized in keywords or key in keyed.get(target_field, frozenset()):
            return target_field
    best_len = 0
    best_target: str | None = None
    for target_field, keywords_k in keyed.items():
        for k in keywords_k:
            if len(k) > best_len and k in key:
                best_len = len(k)
                best_target = target_field
    return best_target


def _fuzzy_match(normalized: str, entity_type: str) -> tuple[str | None, float]:
    """Similitud fuzzy entre nombre normalizado y keywords. Retorna (target_field, ratio)."""
    try:
        from rapidfuzz import fuzz
    except ImportError:
        return None, 0.0

    heuristics = _HEURISTICS.get(entity_type, {})
    # Se compara plegado contra plegado: sin esto un acento contaba como un
    # carácter distinto y bajaba el ratio de un header que es LA MISMA palabra
    # ("comisión" vs "comision"), llegando a caer por debajo del umbral de 0.70.
    n = fold_header(normalized)
    best_target: str | None = None
    best_ratio = 0.0
    for target_field, keywords in heuristics.items():
        for kw in keywords:
            ratio = fuzz.ratio(n, fold_header(kw)) / 100.0
            if ratio > best_ratio:
                best_ratio = ratio
                best_target = target_field
    if best_ratio >= 0.70:
        return best_target, best_ratio
    return None, 0.0


class ColumnMappingService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def suggest_mappings(
        self,
        tenant_id: uuid.UUID,
        entity_type: str,
        headers: list[str],
        sample_rows: list[dict[str, Any]],
        *,
        trace_id: uuid.UUID | str | None = None,
        file_id: uuid.UUID | str | None = None,
        allow_llm: bool = True,
    ) -> list[dict[str, Any]]:
        """Genera sugerencias de mapeo para los headers del archivo.

        FASE 2 (A2): si `trace_id` y `file_id` están presentes, la decisión de la
        4ª capa LLM se traza en pipeline_events (stage="mapping"). Los parámetros
        son keyword-only y opcionales para no romper los callers existentes.

        ``allow_llm=False`` (F7d review) saltea por completo la 4ª capa LLM —
        para callers de solo-lectura/idempotentes (ej. el preview de maestros de
        ``GET /files/{id}/preview``, que puede correr en cada poll/reload) que NO
        deben disparar una llamada real al LLM aunque ``ENABLE_LLM_COLUMN_MAPPING``
        esté prendido. El flujo real de mapeo (``GET /column-mappings``, que el
        usuario dispara explícitamente al armar el mapeo) sigue con el default
        ``True``.
        """
        from app.persistence.models.column_mapping import TenantColumnMapping  # noqa: PLC0415

        # Cargar historial del tenant para este entity_type
        result = await self.db.execute(
            select(TenantColumnMapping).where(
                TenantColumnMapping.tenant_id == tenant_id,
                TenantColumnMapping.entity_type == entity_type,
            )
        )
        history: dict[str, TenantColumnMapping] = {}
        #: El mismo historial indexado por clave PLEGADA (sin acentos ni ñ). El
        #: alias se persiste con la forma acentuada que trajo el archivo que lo
        #: enseñó, así que un tenant que mapeó "Descripción" no encontraba nada
        #: al subir después un archivo con "Descripcion" — la columna volvía a
        #: preguntarse como si nunca la hubiera confirmado. Se resuelve al LEER
        #: en vez de migrar lo escrito: las filas viejas siguen sirviendo y no se
        #: toca dato persistido de ningún tenant.
        history_folded: dict[str, TenantColumnMapping] = {}
        for alias in result.scalars().all():
            history[alias.source_column] = alias
            fk = fold_header(alias.source_column)
            previo = history_folded.get(fk)
            # Dos alias que pliegan igual ("descripcion" y "descripción") pueden
            # coexistir y apuntar a campos distintos. Gana el más confirmado, y
            # a igual confirmaciones el visto más recientemente; el último
            # desempate es alfabético para que el resultado no dependa del orden
            # en que la base devolvió las filas.
            if previo is None or (
                alias.confirmed_count,
                alias.last_seen_at,
                alias.source_column,
            ) > (previo.confirmed_count, previo.last_seen_at, previo.source_column):
                history_folded[fk] = alias

        required = set(REQUIRED_FIELDS.get(entity_type, []))
        suggestions: list[dict[str, Any]] = []
        #: Índices de las columnas cuya lectura no se puede desempatar sin
        #: inventar. Se lleva aparte y no como una clave más del dict porque el
        #: dict se expande con ``**s`` en ``ColumnMappingSuggestion``: una clave
        #: nueva sin declarar en el schema rompe el endpoint.
        sin_desambiguar: set[int] = set()

        for header in headers:
            # La reparación va ANTES de bajar a minúsculas, y no puede hacerse
            # más adentro: la firma del mojibake vive en el caso de sus bytes
            # ("Ã³" es U+00C3 U+00B3) y `_normalize_col` la destruye al pasar a
            # "ã³" (U+00E3), que ya no es reparable. Sobre un encabezado sano es
            # identidad, así que ninguna forma normal cambia de valor.
            normalized = _normalize_col(repair_mojibake(header))

            # Extraer sample values (hasta 5 no-nulos)
            sample_vals: list[str] = []
            for row in sample_rows[:10]:
                v = row.get(header)
                if v is not None and str(v).strip() not in ("", "None", "nan"):
                    sample_vals.append(str(v)[:50])
                if len(sample_vals) >= 5:
                    break

            target_field: str | None = None
            confidence: float = 0.0
            source: str = "none"
            options: tuple[str, ...] = ()
            duda: str | None = None

            # 1. Historial del tenant (prioridad máxima). Coincidencia exacta
            # primero y recién después por clave plegada: si el tenant tiene el
            # alias tal cual vino el header, ese gana sobre cualquier variante.
            aprendido = history.get(normalized) or history_folded.get(fold_header(normalized))
            if aprendido is not None:
                target_field = aprendido.target_field
                confidence = min(0.99, 0.5 + aprendido.confirmed_count / 20.0)
                source = "tenant_history"

            # 2. El reconocedor de encabezados (F-M)
            else:
                lectura = read_header(normalized, entity_type)
                if lectura.outcome == "unico":
                    target_field = lectura.target
                    confidence = 0.75
                    source = "heuristic"
                elif lectura.outcome == "ambiguo" or lectura.duda is not None:
                    options = lectura.options
                    duda = lectura.duda
                    # El reconocedor SÍ entendió el encabezado, y con eso puesto
                    # dijo que no alcanza para elegir. Las capas de abajo saben
                    # MENOS —fuzzy compara contra los keywords crudos, el LLM no
                    # tiene canal para candidatos— así que dejarlas opinar es
                    # cambiar una duda honesta por una respuesta arbitraria.
                    sin_desambiguar.add(len(suggestions))

                # 3. Fuzzy matching: sólo cuando no se reconoció NADA.
                else:
                    fuzzy_target, fuzzy_ratio = _fuzzy_match(normalized, entity_type)
                    if fuzzy_target is not None:
                        target_field = fuzzy_target
                        confidence = fuzzy_ratio * 0.65  # escalar a rango 0–65%
                        source = "fuzzy"

            # Calcular status
            if target_field is not None and target_field != "ignore":
                status = "mapped"
            elif options:
                # Se entendió el encabezado y aun así hay más de una lectura. Es
                # un estado propio: `unmapped` diría que no se reconoció nada.
                status = "ambiguo"
            else:
                status = "unmapped"

            suggestions.append(
                {
                    "source_column": header,
                    "normalized_column": normalized,
                    "sample_values": sample_vals,
                    "target_field": target_field,
                    "confidence": round(confidence, 3),
                    "source": source,
                    "status": status,
                    "options": list(options),
                    "duda": duda,
                    # F-A: los completa la pasada de campos propios / requeridos
                    # de abajo. Se declaran acá para que la forma del dict no
                    # dependa de qué rama corrió — el schema los expande con
                    # `**s` y una clave ausente en unos y presente en otros es
                    # justo lo que hace divergir a los consumidores.
                    "target_label": None,
                    "missing_field": None,
                }
            )

        # FASE 2: 4ª capa LLM (fallback). Solo para columnas con baja confianza
        # determinística. Una sola llamada batch; fail-silent (flag/key/errores).
        # `allow_llm=False` la saltea por completo (ver docstring de este método).
        if allow_llm:
            await self._apply_llm_fallback(
                entity_type,
                suggestions,
                tenant_id=tenant_id,
                trace_id=trace_id,
                file_id=file_id,
                skip=sin_desambiguar,
            )

        # Segunda pasada: detectar required_missing
        #
        # V10 — acá NO hace falta filtrar por target canónico, y conviene dejar
        # escrito por qué: se probó a agregarlo y ninguna mutación lo mata. La
        # cobertura es una resta de conjuntos entre nombres de campo, y
        # `"custom_field:amount"` nunca es igual a `"amount"` — un campo propio
        # no puede colarse como requerido cubierto por más que se llame igual.
        # Filtrar sería código defensivo inalcanzable disfrazado de protección.
        #
        # Lo que SÍ sostiene V10 con F-A puesto es la regla de abajo: la columna
        # candidata a un requerido no se auto-propone como campo propio. Si se
        # auto-propusiera, no quedaría ninguna `unmapped` donde poner la marca.
        # Y río abajo el confirm valida con `missing_required_fields`, que sí
        # filtra explícitamente porque ahí el caller le pasa targets mezclados.
        mapped_targets = {s["target_field"] for s in suggestions if s["status"] == "mapped"}
        missing_required = required - mapped_targets

        # Si hay campos requeridos sin cubrir, marcar la primera columna sin mapear
        # cuyo nombre normalizado se acerque a algún campo requerido
        if missing_required:
            for s in suggestions:
                if s["status"] == "unmapped":
                    norm = s["normalized_column"]
                    for req_field in list(missing_required):
                        # Check si algún keyword del required field está en el nombre
                        req_keywords = _HEURISTICS.get(entity_type, {}).get(req_field, set())
                        if any(k in norm for k in req_keywords):
                            s["status"] = "required_missing"
                            # Qué campo falta, no sólo que falta algo: el estado
                            # describe el CAMPO DESTINO, y sin nombrarlo la
                            # pantalla tiene que adivinar cuál de los requeridos
                            # es este punto rojo.
                            s["missing_field"] = req_field
                            missing_required.discard(req_field)
                            break

        # F-A — preservar primero, clasificar después.
        #
        # Lo que no se reconoce deja de desaparecer detrás de un «Sin mapear»:
        # se propone conservarlo como campo propio, con el NOMBRE ORIGINAL de la
        # columna como etiqueta. La queja que abrió la fase era tener que
        # renombrar prácticamente todas las columnas de un archivo real.
        #
        # Va DESPUÉS del LLM y de la pasada de requeridos, y saltea dos casos a
        # propósito:
        #  - cualquier columna con `duda`: el reconocedor entendió el encabezado
        #    y tiene algo que decir — sea que hay más de una lectura (`ambiguo`)
        #    o que esta hoja no tiene campo donde poner ese concepto, que llega
        #    como `unmapped` CON duda. Archivarla como campo propio taparía la
        #    explicación, y rompería el invariante de F-M de que una columna
        #    `mapped` no arrastra una duda (hay tests que lo fijan).
        #  - `required_missing`: esta columna es la candidata a un requerido sin
        #    cubrir. Proponer «guardala como campo propio» es ofrecer tirar la
        #    fecha de la venta a un campo suelto — el default tiene que ser que
        #    la persona decida, no que Véktor la archive.
        #
        # Y no propone sin una sola muestra: una columna sin valores no es un
        # dato a conservar, es una columna vacía (el resto de esa política vive
        # en el confirm, que dropea las 100% vacías del archivo completo).
        slugs_usados: set[str] = {
            parse_target(s["target_field"]).field
            for s in suggestions
            if parse_target(s["target_field"]).kind == "custom"
        }
        for s in suggestions:
            if s["status"] != "unmapped" or not s["sample_values"] or s["duda"]:
                continue
            slug = custom_field_slug(s["source_column"])
            if slug is None:
                continue
            # Desambiguación determinística por orden de aparición: "Obs." y
            # "Obs" dan el mismo slug, y sin sufijo la segunda columna pisaría a
            # la primera. El 422 de F-0 sigue siendo el cinturón duro; esto evita
            # llegar a él por una colisión que Véktor mismo se creó.
            if slug in slugs_usados:
                n = 2
                while f"{slug}_{n}" in slugs_usados:
                    n += 1
                slug = f"{slug}_{n}"
            slugs_usados.add(slug)
            s["target_field"] = f"custom_field:{slug}"
            # La etiqueta viaja aparte y NO se reconstruye desde el slug: el
            # slug pierde acentos, mayúsculas y puntuación, así que "Año Fiscal"
            # volvería como "ano fiscal". Es lo único con lo que la persona
            # reconoce su columna en el ERD y en la pantalla de campos propios.
            s["target_label"] = s["source_column"]
            s["status"] = "mapped"
            s["source"] = "auto_custom"

        return suggestions

    async def _apply_llm_fallback(
        self,
        entity_type: str,
        suggestions: list[dict[str, Any]],
        *,
        tenant_id: uuid.UUID | None = None,
        trace_id: uuid.UUID | str | None = None,
        file_id: uuid.UUID | str | None = None,
        skip: set[int] | None = None,
    ) -> None:
        """FASE 2: mejora las sugerencias de baja confianza con el LLM (in-place).

        Si `trace_id`/`file_id` están presentes, emite un pipeline_event con la
        traza antes/después de cada columna evaluada (qué decidió lo determinístico,
        qué decidió el LLM, y si lo pisó).

        ``skip`` son los índices que el reconocedor ya leyó y declaró indecidibles
        (F-M). Baja confianza y ambigüedad no son lo mismo: la primera dice «no sé»
        y el LLM puede ayudar; la segunda dice «entendí, y con eso entendido sigue
        habiendo dos lecturas». Una respuesta del LLM no es demostración de la
        intención del usuario, que es lo único que la regla de la fase acepta.
        """
        from app.application.services.llm_column_mapper import (  # noqa: PLC0415
            LLM_MAPPING_THRESHOLD,
            suggest_with_llm,
        )

        omitir = skip or set()
        low_conf = [
            s
            for i, s in enumerate(suggestions)
            if s["confidence"] < LLM_MAPPING_THRESHOLD and i not in omitir
        ]
        if not low_conf:
            return
        valid_fields = CANONICAL_FIELDS.get(entity_type, {})
        if not valid_fields:
            return

        # Snapshot "antes" (la decisión determinística) para auditar qué pisó el LLM.
        before = {
            s["source_column"]: {
                "target_field": s["target_field"],
                "confidence": s["confidence"],
                "source": s["source"],
            }
            for s in low_conf
        }

        llm_result = await suggest_with_llm(
            entity_type,
            [{"header": s["source_column"], "sample_values": s["sample_values"]} for s in low_conf],
            valid_fields,
        )
        if not llm_result:
            return

        decisions: list[dict[str, Any]] = []
        for s in low_conf:
            hit = llm_result.get(s["source_column"])
            prev = before[s["source_column"]]
            overwritten = False
            if hit:
                target = hit["target_field"]
                conf = hit["confidence"]
                # Solo pisar si el LLM aporta un mapeo usable y MÁS confiable.
                if target != "ignore" and conf > s["confidence"]:
                    s["target_field"] = target
                    s["confidence"] = round(conf, 3)
                    s["source"] = "llm"
                    s["status"] = "mapped"
                    # Una columna resuelta no puede seguir explicando por qué no
                    # se podía resolver. Hoy es inalcanzable —las que tienen duda
                    # están en `skip`— pero el invariante «mapped ⇒ sin duda» se
                    # sostiene acá, que es el único lugar que puede romperlo.
                    s["options"] = []
                    s["duda"] = None
                    overwritten = True
            decisions.append(
                {
                    "column": s["source_column"],
                    "deterministic_target": prev["target_field"],
                    "deterministic_confidence": prev["confidence"],
                    "source_before": prev["source"],
                    "llm_target": hit["target_field"] if hit else None,
                    "llm_confidence": hit["confidence"] if hit else None,
                    "source_after": s["source"],
                    "final_target": s["target_field"],
                    "final_confidence": s["confidence"],
                    "overwritten": overwritten,
                }
            )

        await self._emit_mapping_event(
            tenant_id=tenant_id,
            trace_id=trace_id,
            file_id=file_id,
            entity_type=entity_type,
            decisions=decisions,
        )

    async def _emit_mapping_event(
        self,
        *,
        tenant_id: uuid.UUID | None,
        trace_id: uuid.UUID | str | None,
        file_id: uuid.UUID | str | None,
        entity_type: str,
        decisions: list[dict[str, Any]],
    ) -> None:
        """Traza la decisión del LLM de mapeo en pipeline_events (fail-silent).

        No emite si falta `trace_id`/`file_id`/`tenant_id` (callers que no pasan
        contexto de traza, p.ej. tests unitarios del mapeo)."""
        if trace_id is None or file_id is None or tenant_id is None:
            return
        from app.application.services import pipeline_event_service  # noqa: PLC0415
        from app.persistence.models.pipeline_event import STAGE_MAPPING  # noqa: PLC0415

        overwritten_count = sum(1 for d in decisions if d["overwritten"])
        await pipeline_event_service.emit_event(
            self.db,
            trace_id=trace_id,
            tenant_id=tenant_id,
            stage=STAGE_MAPPING,
            file_id=file_id,
            detail={
                "type": "column_mapping",
                "entity_type": entity_type,
                "columns_evaluated": len(decisions),
                "columns_overwritten": overwritten_count,
                "decisions": decisions,
            },
        )

    async def save_mappings(
        self,
        tenant_id: uuid.UUID,
        entity_type: str,
        confirmed: list[dict[str, str]],
    ) -> None:
        """Upsert de mapeos confirmados en tenant_column_mappings.

        No aprende mapeos "ignore" — cada archivo puede tener columnas distintas
        que ignorar. No aprende custom_fields tampoco (demasiado específicos).
        """
        from app.persistence.models.column_mapping import TenantColumnMapping  # noqa: PLC0415

        now = datetime.now(tz=UTC)

        # Una columna aparece TANTAS veces como hojas de esta entidad la traigan:
        # un libro con Compras_Mercaderia + Compras_Insumos + Gastos_Fijos manda
        # tres `fecha`. Procesarlas de a una rompía de dos formas distintas:
        #
        #  1. El `SELECT` de abajo no ve la fila que la vuelta anterior dejó
        #     PENDIENTE (producción corre con `autoflush=False`), así que insertaba
        #     una segunda con la misma clave → UniqueViolationError y 500 al
        #     confirmar. Es leer lo que uno mismo acaba de escribir.
        #  2. Aun sin reventar, `confirmed_count` subía una vez por hoja: un solo
        #     archivo le daba a un alias la confianza de tres archivos distintos.
        #
        # Tres hojas del mismo archivo son UNA confirmación. Y si no coinciden en
        # el destino, la columna NO se aprende: evidencia contradictoria dentro de
        # un mismo archivo es ambigüedad, no una preferencia — quedarse con una
        # sería elegir por orden de hoja, el last-wins silencioso que este
        # pipeline existe para evitar.
        # El filtro de no-aprendibles va ANTES de buscar contradicciones: si una
        # hoja manda `fecha → expense_date` y otra `fecha → ignore`, no hay
        # conflicto que resolver. Ignorar una columna en una hoja no dice nada
        # sobre qué significa esa columna, y tratarlo como contradicción
        # descartaba el mapeo bueno y dejaba el alias ya aprendido sin refrescar.
        # Un conflicto real es entre dos destinos REALES.
        colapsado: dict[str, str | None] = {}
        for mapping in confirmed:
            tgt = mapping["target_field"]
            if parse_target(tgt).kind in ("ignore", "none", "custom"):
                continue
            col = _normalize_col(mapping["source_column"])
            if col in colapsado and colapsado[col] != tgt:
                colapsado[col] = None  # contradicción: no se aprende
            elif col not in colapsado:
                colapsado[col] = tgt

        for source_col, target in colapsado.items():
            if target is None:
                logger.info(
                    "column_mapping.learning_skipped_conflict",
                    entity_type=entity_type,
                    source_column=source_col,
                )
                continue

            result = await self.db.execute(
                select(TenantColumnMapping).where(
                    TenantColumnMapping.tenant_id == tenant_id,
                    TenantColumnMapping.entity_type == entity_type,
                    TenantColumnMapping.source_column == source_col,
                )
            )
            existing = result.scalar_one_or_none()

            if existing:
                if existing.target_field != target:
                    # Usuario cambió el mapeo → reiniciar contador
                    existing.target_field = target
                    existing.confirmed_count = 1
                else:
                    existing.confirmed_count += 1
                existing.last_seen_at = now
            else:
                self.db.add(
                    TenantColumnMapping(
                        id=uuid.uuid4(),
                        tenant_id=tenant_id,
                        entity_type=entity_type,
                        source_column=source_col,
                        target_field=target,
                        confirmed_count=1,
                        last_seen_at=now,
                        created_at=now,
                    )
                )

        await self.db.flush()
        logger.info(
            "column_mapping.saved",
            tenant_id=str(tenant_id),
            entity_type=entity_type,
            count=len(confirmed),
        )

    async def get_learned_mappings(self, tenant_id: uuid.UUID) -> list[Any]:
        """Retorna todos los mapeos aprendidos del tenant, ordenados por entity_type + source."""
        from app.persistence.models.column_mapping import TenantColumnMapping  # noqa: PLC0415

        result = await self.db.execute(
            select(TenantColumnMapping)
            .where(TenantColumnMapping.tenant_id == tenant_id)
            .order_by(TenantColumnMapping.entity_type, TenantColumnMapping.source_column)
        )
        return list(result.scalars().all())

    async def delete_mapping(
        self, tenant_id: uuid.UUID, mapping_id: uuid.UUID
    ) -> bool:
        """Elimina un mapeo aprendido. Retorna True si existía."""
        from app.persistence.models.column_mapping import TenantColumnMapping  # noqa: PLC0415

        result = await self.db.execute(
            select(TenantColumnMapping).where(
                TenantColumnMapping.id == mapping_id,
                TenantColumnMapping.tenant_id == tenant_id,
            )
        )
        existing = result.scalar_one_or_none()
        if not existing:
            return False
        await self.db.delete(existing)
        await self.db.flush()
        return True
