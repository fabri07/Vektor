"""Qué filas de ``sales_entries`` son ventas y cuáles son cobros de fiado (T1).

``sales_entries`` guarda dos cosas distintas:

- **ventas** (contado, tarjeta, fiado, mixto…): son ingreso el día que se hacen;
- **cobros de fiado** (``payment_method = "inflow"``): la plata de una venta
  fiada que ya se contó. Entran a caja y bajan la deuda del cliente, pero NO son
  ingreso: sumarlos contaba dos veces cada venta fiada que se cobra (revisión
  profunda 2026-10-24, hallazgo T1).

Todo lector que agregue ``SaleEntry.amount`` como venta usa ``es_venta()``; el
que mide plata que entra a caja suma los cobros con ``es_cobro_de_fiado()``.
Se compara sin distinguir mayúsculas, como ``respaldo_service.es_cobro_de_fiado``
y FactsService.
"""

from sqlalchemy import func
from sqlalchemy.sql import ColumnElement

from app.domain.transaction import INFLOW_PAYMENT_METHOD
from app.persistence.models.transaction import SaleEntry


def es_cobro_de_fiado() -> ColumnElement[bool]:
    """Predicado WHERE: la fila es un cobro de fiado, no una venta."""
    return func.lower(SaleEntry.payment_method) == INFLOW_PAYMENT_METHOD


def es_venta() -> ColumnElement[bool]:
    """Predicado WHERE: la fila es una venta (todo lo que no es un cobro).

    ``payment_method`` es NOT NULL, así que el ``!=`` no descarta nulos.
    """
    return func.lower(SaleEntry.payment_method) != INFLOW_PAYMENT_METHOD
