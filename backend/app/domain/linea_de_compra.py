"""T3 · B-3 — qué gasto es parte de una compra de mercadería.

Una compra es un movimiento ``purchase`` (stock, costo, respaldo de ventas) más un
gasto de mercadería por línea, sin FK entre los dos. Anular o editar el gasto
suelto deja el movimiento vivo: el stock, el costo y el respaldo siguen como si la
compra existiera. Hasta que exista la anulación de la compra entera (decisión del
dueño 2026-10-26: antes del 30/10 sólo se bloquea), esos gastos no se anulan ni se
les cambia monto, categoría, tipo, fecha ni proveedor: ni por ``DELETE``/``PATCH /expenses``
ni por ``RECLASSIFY_EXPENSE`` del chat. Esta es la ÚNICA definición; la usan los
tres caminos y la respuesta de la API (``is_purchase_line``) para la pantalla.

Es línea de compra:

- todo gasto con ``custom_fields.purchase_group_id``: lo escribe
  ``compra_service.registrar_compra`` (compra a mano, remito sin archivo,
  «Otros» y chat) en cada línea **y en el envío aparte**. El envío es OPEX y no
  toca el costo, pero forma parte del mismo comprobante: anularlo suelto dejaría
  una compra a medias que la anulación de la compra entera no podría reconstruir.
- todo gasto de mercadería (``INVENTORY`` o ``COGS``) con producto: así nacen
  las líneas importadas (libro de compras, remito, catálogo tomado como compra),
  que no llevan grupo.

Sin sesión ni ORM: recibe los valores del gasto.
"""

from __future__ import annotations

from typing import Any, Final

from app.domain.expense_categories import normalize_expense_category

CODIGO_LINEA_DE_COMPRA: Final = "PURCHASE_LINE"

MENSAJE_LINEA_DE_COMPRA: Final = (
    "Este gasto es parte de una compra de mercadería: anularlo o cambiarle el "
    "monto, la categoría, la fecha o el proveedor dejaría el stock y el costo como "
    "si la compra siguiera. Si la mercadería que entró está mal, corregila con un "
    "ajuste de stock desde el producto. La anulación de compras todavía no existe. "
    "La descripción y las notas sí se pueden editar."
)

#: La clave que agrupa las líneas (y el envío) de un comprobante.
CLAVE_GRUPO_DE_COMPRA: Final = "purchase_group_id"


def es_linea_de_compra(
    *,
    product_id: object | None,
    category: str | None,
    expense_type: str | None,
    custom_fields: dict[str, Any] | None,
) -> bool:
    """``True`` si el gasto es una línea (o el envío) de una compra de mercadería."""
    if isinstance(custom_fields, dict) and custom_fields.get(CLAVE_GRUPO_DE_COMPRA):
        return True
    if product_id is None:
        return False
    if expense_type == "COGS":
        return True
    return normalize_expense_category(category)[0] == "INVENTORY"


def es_linea_de_compra_el_gasto(gasto: Any) -> bool:
    """Lo mismo, leyendo los atributos de un ``ExpenseEntry`` (o su respuesta)."""
    return es_linea_de_compra(
        product_id=getattr(gasto, "product_id", None),
        category=getattr(gasto, "category", None),
        expense_type=getattr(gasto, "expense_type", None),
        custom_fields=getattr(gasto, "custom_fields", None),
    )
