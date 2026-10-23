"""Qué gastos entran en un agregado de RESULTADO y cuáles no.

El corte no es «todos los agregados»: es **resultado vs caja**, y elegir mal
produce un número que cierra pero miente.

El caso que obliga a distinguirlos es el flete de una compra que se capitalizó en
el costo del stock (F-H6.d). Esa plata SÍ salió de la caja, y el `ExpenseEntry`
existe —tiene que existir, o la reversa del archivo no tendría qué revertir—, pero
su importe ya está adentro del valor del inventario. Descontarlo del resultado
*además* de tenerlo capitalizado lo cuenta dos veces:

    $100 de mercadería + $10 de flete, mismo comprobante
      no distribuir  → gastos 110 · stock 100     el 10 vive en el resultado
      por subtotal   → gastos 100 · stock 110     el 10 vive en el activo
      por subtotal, sin este filtro
                     → gastos 110 · stock 110     el 10 vive en los dos

Regla para el que escriba el próximo agregado:

- **Agregado de RESULTADO** (margen neto, gastos del período, gastos por
  categoría, health score): usa ``gasto_de_resultado()``. Convive con el valor
  del stock en la misma respuesta, así que no puede contar dos veces lo mismo.
- **Agregado de CAJA** (arqueo, flujo neto, salidas de efectivo, proyección):
  NO lo usa. El dinero salió de la caja el día que salió, y esconderlo ahí sería
  el error simétrico — un arqueo que no cuadra con lo que hay en el cajón.
"""

from typing import Any

from sqlalchemy.sql import ColumnElement

from app.domain.purchase_cost import ATRIBUIDO_A_INVENTARIO_FIELD
from app.persistence.repositories._jsonb_flags import flag_not_true_sql


def gasto_de_resultado(custom_fields_column: Any) -> ColumnElement[bool]:
    """Predicado WHERE: el gasto NO está ya capitalizado en el valor del stock.

    Las filas comunes no tienen la clave, y ``flag_not_true_sql`` las deja pasar
    (su ``coalesce`` existe justamente para eso): sin el filtro correcto un
    ``NULL NOT IN (...)`` descartaría TODOS los gastos del período.
    """
    return flag_not_true_sql(custom_fields_column, ATRIBUIDO_A_INVENTARIO_FIELD)
