"""Cómo se le muestra a un humano un stock contado en unidades base.

``products.stock_units`` y ``sales_entries.quantity`` cuentan **unidades base**:
un producto en gramos con factor 1000 tiene ``stock_units = 5000`` para 5 kg.
Esa decisión mantiene intacta toda la aritmética —motor de inventario, chequeo
de integridad, reconciliación temporal, FactsService— porque todo es consistente
en la misma unidad. El precio es el de la presentación: ``5000`` gramos es un
número correcto y ``5000`` kilos es un desastre.

Contrato de precios: ``sale_price_ars`` y ``unit_cost_ars`` son por UNIDAD DE
VENTA (por kg, por litro, por unidad); ``stock_units`` y las cantidades, en
unidades base. Todo lo que multiplique stock por costo tiene que dividir por el
factor — :func:`valor_de_stock` es la única forma de hacerlo.

El resto del módulo es de PRESENTACIÓN y nada que haga cuentas debe llamarlo.
"""

from __future__ import annotations

from decimal import Decimal

#: Los valores admitidos por el CHECK de `products.sale_unit`.
SALE_UNITS = ("unit", "gram", "milliliter")

#: Etiqueta por (unidad base, cuántas unidades base entran en una de venta).
#: Deliberadamente corto: sólo las combinaciones cuyo nombre se CONOCE. Un
#: factor de 500 no tiene nombre ("medio kilo" no es una unidad), y bautizarlo
#: sería inventar una unidad que el negocio no usa.
_ETIQUETAS: dict[tuple[str, int], str] = {
    ("unit", 1): "un.",
    ("gram", 1): "g",
    ("gram", 1000): "kg",
    ("milliliter", 1): "ml",
    ("milliliter", 1000): "L",
}

#: Etiqueta de la unidad base pelada, para caer cuando el factor no tiene nombre.
_BASE: dict[str, str] = {"unit": "un.", "gram": "g", "milliliter": "ml"}


def etiqueta_de_unidad(sale_unit: str, base_units_per_sale_unit: int) -> str:
    """Cómo se llama la unidad en la que se muestra la cantidad."""
    conocida = _ETIQUETAS.get((sale_unit, base_units_per_sale_unit))
    if conocida is not None:
        return conocida
    # Factor sin nombre: se muestra en unidad base, que siempre es correcto.
    return _BASE.get(sale_unit, "un.")


def _formato_ar(valor: Decimal, decimales: int) -> str:
    """Número en convención argentina: '.' para miles y ',' para decimales."""
    texto = f"{valor:,.{decimales}f}"
    # `,` y `.` de Python están al revés de la convención local: se intercambian
    # en un solo paso con un centinela para no pisar uno con el otro.
    return texto.replace(",", "\x00").replace(".", ",").replace("\x00", ".")


def formatear_cantidad(
    unidades_base: int, sale_unit: str, base_units_per_sale_unit: int
) -> str:
    """Unidades base → texto con su unidad. Ej.: 750 g con factor 1000 → '0,750 kg'.

    Los decimales salen del factor (1000 → 3), no se recortan: una balanza que
    imprime "0,75 kg" y otra "0,750 kg" describen lo mismo, pero al comparar un
    ticket con el stock la cantidad de decimales es lo que deja ver si el
    redondeo se comió algo.
    """
    etiqueta = etiqueta_de_unidad(sale_unit, base_units_per_sale_unit)
    if _ETIQUETAS.get((sale_unit, base_units_per_sale_unit)) is None:
        # Sin nombre para el factor, la cantidad se muestra en unidad base tal
        # cual: convertirla exigiría una unidad que no se sabe nombrar.
        return f"{_formato_ar(Decimal(unidades_base), 0)} {etiqueta}"

    if base_units_per_sale_unit == 1:
        return f"{_formato_ar(Decimal(unidades_base), 0)} {etiqueta}"

    decimales = len(str(base_units_per_sale_unit)) - 1
    valor = Decimal(unidades_base) / Decimal(base_units_per_sale_unit)
    return f"{_formato_ar(valor, decimales)} {etiqueta}"


def valor_de_stock(
    unidades_base: int, base_units_per_sale_unit: int, costo_por_unidad_de_venta: Decimal
) -> Decimal:
    """Valor del stock: unidades base × costo por unidad de venta / factor.

    Sin la división, 5 kg (5000 g) de un producto que cuesta $800 el kilo valían
    $4.000.000 en vez de $4.000.
    """
    factor = base_units_per_sale_unit if base_units_per_sale_unit > 0 else 1
    return (Decimal(unidades_base) * costo_por_unidad_de_venta / factor).quantize(Decimal("0.01"))
