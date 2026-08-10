"""Batería de caracterización: qué responde HOY el reconocedor de encabezados.

Este archivo no dice qué *debería* pasar — dice qué pasa. Existe porque F-M
reemplaza la capa heurística de mapeo, que es la de mayor prioridad automática, y
sin una foto previa el cambio de comportamiento no es revisable: las ~40
aserciones que ya existían están repartidas en cinco archivos, prueban headers
sueltos y ninguna dice qué pasa con uno compuesto.

**Los veredictos son datos, no aserciones.** Cada fila lleva el target que el
motor devuelve y una etiqueta de si eso está bien. Las filas `MAL` están acá a
propósito: son el daño que F-M viene a arreglar, y tenerlas escritas es lo que
convierte el rediseño en un diff legible en vez de un efecto lateral.

El modo de falla que documentan: `_heuristic_match` elige el substring más largo,
sin noción de qué palabra es el sustantivo núcleo y cuál lo modifica. Cuatro de
los errores de abajo son **empates de longitud resueltos por el orden de
declaración del dict** — el propio docstring de la función lo admite.
"""

from __future__ import annotations

import pytest

from app.application.services.column_mapping_service import _heuristic_match, _normalize_col

#: Veredicto de cada fila. No cambia lo que el test asserta (siempre el valor
#: medido); clasifica para poder contar.
OK = "ok"  # el target es el correcto
MAL = "mal"  # resuelve a un concepto distinto del que nombra el header
FALTA = "falta"  # no sugiere nada y debería
SIN_CAMPO = "sin_campo"  # no sugiere nada, y está bien: el campo no existe
AMBIGUO = "ambiguo"  # elige uno cuando el header admite más de una lectura

# (entidad, encabezado, target que devuelve HOY, veredicto, por qué)
BATERIA: list[tuple[str, str, str | None, str, str]] = [
    # ── venta ────────────────────────────────────────────────────────────────
    ("sale", "Fecha", "transaction_date", OK, ""),
    (
        "sale",
        "Fecha de venta",
        "amount",
        MAL,
        "empate `venta`(5) vs `fecha`(5): la columna de FECHA entra como monto",
    ),
    ("sale", "Monto", "amount", OK, ""),
    ("sale", "Importe", "amount", OK, ""),
    ("sale", "Total", "amount", OK, ""),
    ("sale", "Precio unitario", "unit_price", OK, ""),
    (
        "sale",
        "Precio de venta",
        "amount",
        AMBIGUO,
        "en una venta puede ser el precio de cada unidad o el total de la línea",
    ),
    ("sale", "Cantidad", "quantity", OK, ""),
    ("sale", "Producto", "product_name", OK, ""),
    ("sale", "Artículo", None, FALTA, "el vocabulario dice `articulo`, sin tilde"),
    ("sale", "Cliente", "customer_name", OK, ""),
    ("sale", "DNI", "customer_dni", OK, ""),
    ("sale", "CUIT", "customer_cuit", OK, ""),
    ("sale", "Email", "customer_email", OK, ""),
    ("sale", "Teléfono", "customer_phone", OK, ""),
    ("sale", "Forma de pago", "payment_method", OK, ""),
    ("sale", "Método de pago", "payment_method", OK, ""),
    ("sale", "Observaciones", "notes", OK, ""),
    ("sale", "Notas", "notes", OK, ""),
    # ── gasto / compra ───────────────────────────────────────────────────────
    ("expense", "Fecha", "expense_date", OK, ""),
    (
        "expense",
        "Fecha del gasto",
        "amount",
        MAL,
        "empate `gasto`(5) vs `fecha`(5): el header más común de una planilla de "
        "gastos resuelve a monto",
    ),
    ("expense", "Monto", "amount", OK, ""),
    ("expense", "Gasto", "amount", OK, ""),
    ("expense", "Costo", "amount", OK, ""),
    ("expense", "Compra", "amount", OK, ""),
    ("expense", "Total", "amount", OK, ""),
    ("expense", "Categoría", None, FALTA, "el vocabulario dice `categoria`, sin tilde"),
    ("expense", "Rubro", "category", OK, ""),
    ("expense", "Proveedor", "supplier_name", OK, ""),
    ("expense", "Forma de pago", "payment_method", OK, ""),
    ("expense", "Recurrente", "is_recurring", OK, ""),
    ("expense", "Cantidad", "quantity", OK, ""),
    ("expense", "Precio unitario", "unit_price", OK, ""),
    ("expense", "Producto", "product_name", OK, ""),
    ("expense", "SKU", "sku", OK, ""),
    ("expense", "Código de barras", "barcode", OK, ""),
    ("expense", "Nro comprobante", "invoice_number", OK, ""),
    ("expense", "Nro factura", "invoice_number", OK, ""),
    ("expense", "Remito", "invoice_number", OK, ""),
    ("expense", "Envío", None, FALTA, "el vocabulario dice `envio`, sin tilde"),
    ("expense", "Flete", "shipping_cost", OK, ""),
    ("expense", "Descuento", None, SIN_CAMPO, "`discount` todavía no existe"),
    ("expense", "Bonificación", None, SIN_CAMPO, "ídem"),
    ("expense", "IVA", None, SIN_CAMPO, "`taxes` todavía no existe"),
    ("expense", "Impuestos", None, SIN_CAMPO, "ídem"),
    (
        "expense",
        "Bonificación proveedor",
        "supplier_name",
        MAL,
        "`proveedor`(9) le gana: un descuento se convierte en el nombre del proveedor",
    ),
    (
        "expense",
        "Total factura sin impuestos",
        "amount",
        AMBIGUO,
        "acierta de casualidad: es el total del COMPROBANTE, no el de la línea, y "
        "en cuanto exista `taxes` el `impuestos`(9) le va a ganar a `total`(5)",
    ),
    (
        "expense",
        "Envío unitario",
        "unit_price",
        MAL,
        "`unitario`(8) le gana a `envio`(5): el flete entra como precio de compra "
        "y corrompe el costo del producto",
    ),
    ("expense", "Precio con IVA", None, FALTA, "es un precio; hoy no sugiere nada"),
    (
        "expense",
        "Costo final por producto",
        "product_name",
        MAL,
        "`producto`(8) le gana a `costo`(5): un costo entra como nombre",
    ),
    ("expense", "Precio sin IVA", None, FALTA, "es un precio"),
    ("expense", "Neto sin IVA", None, FALTA, "es un monto"),
    (
        "expense",
        "Flete por línea",
        "shipping_cost",
        AMBIGUO,
        "es el flete de la línea, no el del comprobante; los dos se leen con "
        "reglas opuestas (uno se colapsa, el otro se suma)",
    ),
    (
        "expense",
        "Descuento por producto",
        "product_name",
        MAL,
        "`producto`(8) le gana a `descuento`: mismo caso que la bonificación",
    ),
    ("expense", "Fecha de comprobante", "expense_date", OK, ""),
    # ── catálogo de productos ────────────────────────────────────────────────
    ("product", "SKU", "sku", OK, ""),
    ("product", "Código", "sku", OK, ""),
    (
        "product",
        "Código de barras",
        "sku",
        MAL,
        "empate `código`(6) vs `barras`(6): el código de barras entra como SKU. "
        "Sin la tilde el header matchea exacto y resuelve bien — con tilde, no",
    ),
    ("product", "EAN", "barcode", OK, ""),
    ("product", "Nombre", "name", OK, ""),
    ("product", "Producto", "name", OK, ""),
    ("product", "Precio de venta", "sale_price_ars", OK, ""),
    ("product", "Precio de lista", "list_price_ars", OK, ""),
    ("product", "Precio de compra", "unit_cost_ars", OK, "incidente ASTERIA, ya arreglado"),
    ("product", "Precio unitario", "unit_cost_ars", OK, ""),
    ("product", "Costo", "unit_cost_ars", OK, ""),
    ("product", "Costo unitario", "unit_cost_ars", OK, ""),
    ("product", "Stock", "stock_units", OK, ""),
    ("product", "Categoría", None, FALTA, "tilde"),
    (
        "product",
        "Descripción",
        "name",
        MAL,
        "`descripción`(11) figura en el set de `name` Y en el de `description`: "
        "empate, gana el orden del dict",
    ),
    ("product", "Vencimiento", "expiry_date", OK, ""),
    ("product", "Precio sugerido", "list_price_ars", OK, ""),
    ("product", "Marca", None, SIN_CAMPO, "no hay campo de marca en el catálogo"),
    # ── maestros ─────────────────────────────────────────────────────────────
    ("customer", "Nombre", "name", OK, ""),
    ("customer", "Apellido", "last_name", OK, ""),
    ("customer", "DNI", "dni", OK, ""),
    ("customer", "CUIT", "cuit", OK, ""),
    ("customer", "Email", "email", OK, ""),
    ("customer", "Teléfono", "phone", OK, ""),
    ("customer", "Localidad", "locality", OK, ""),
    ("customer", "Provincia", "province", OK, ""),
    ("customer", "Dirección", "address", OK, ""),
    ("customer", "Código postal", "postal_code", OK, ""),
    ("customer", "Cumpleaños", "birthday", OK, ""),
    ("supplier", "Nombre", "name", OK, ""),
    ("supplier", "Apellido", "last_name", OK, ""),
    ("supplier", "CUIL", "cuil", OK, ""),
    ("supplier", "Email", "email", OK, ""),
    ("supplier", "Teléfono", "phone", OK, ""),
    ("supplier", "Forma de pago", "payment_method", OK, ""),
]


@pytest.mark.parametrize(
    ("entity", "header", "esperado"),
    [(e, h, t) for e, h, t, _, _ in BATERIA],
    ids=[f"{e}:{h}" for e, h, _, _, _ in BATERIA],
)
def test_lo_que_responde_hoy(entity: str, header: str, esperado: str | None) -> None:
    """Foto del comportamiento actual. Verde hoy por definición.

    Cuando F-M cambie una de estas respuestas, este test se pone rojo y la
    corrección de la fila ES la revisión del cambio.
    """
    assert _heuristic_match(_normalize_col(header), entity) == esperado


def test_el_tamano_del_problema_esta_medido() -> None:
    """Cuántos encabezados de la batería el motor lee mal.

    Un solo número que resume la foto. Si alguien arregla uno sin querer, o
    rompe otro, el conteo se mueve y hay que mirar por qué — que es más de lo
    que hoy detecta cualquier test del repo.
    """
    conteo: dict[str, int] = {}
    for _, _, _, veredicto, _ in BATERIA:
        conteo[veredicto] = conteo.get(veredicto, 0) + 1

    assert conteo[MAL] == 8
    assert conteo[FALTA] == 7
    assert conteo[AMBIGUO] == 3
    assert conteo[SIN_CAMPO] == 5
    assert conteo[OK] == 67
    assert len(BATERIA) == 90


def test_todo_veredicto_no_ok_explica_por_que() -> None:
    """Una fila marcada mal sin motivo es una afirmación sin evidencia: dentro de
    seis meses nadie sabe si era un bug o una decisión."""
    for entity, header, _, veredicto, motivo in BATERIA:
        if veredicto != OK:
            assert motivo.strip(), f"{entity}:{header} está marcado {veredicto} sin motivo"
