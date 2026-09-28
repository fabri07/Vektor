"""Qué pagos de caja ve un lector de plata, y cuáles ya vio por la venta (B9).

Una operación de caja con **un solo método** escribe ese método en cada una de
sus ``SaleEntry``, así que cualquier lector que clasifique por
``sales_entries.payment_method`` la lee bien sin saber que existe la caja.

Una operación **mixta** —más de un método DISTINTO entre sus tenders; dos pagos
en efectivo siguen siendo una venta de efectivo— no tiene un método que escribir
en la línea: $600 en efectivo y $400 fiados no son ni una cosa ni la otra. Sus
líneas llevan el marcador ``mixed`` y el importe de cada medio vive en
``pos_tenders``. Por eso un lector de plata suma dos poblaciones DISJUNTAS:

    A · las ventas vivas con método distinto de ``mixed``   → ``venta_no_mixta()``
    B · una fila por tender de cada operación mixta viva     → ``tenders_mixtos()``

Cada peso cae en un solo lado. Regla para el que escriba el próximo lector:

- **No unir ``pos_tenders`` con ``pos_operation_lines``.** Tres líneas y dos
  pagos dan seis filas: los importes salen triplicados. La proyección de acá
  devuelve una fila por tender y la procedencia se resuelve con una subconsulta
  de UNA fila por operación.
- **La fecha y el cliente salen de la cabecera** (``operation_date`` y
  ``customer_id``), que son los mismos que el writer copia en cada línea.
- **Aplicar a B el mismo filtro que se le aplica a A** (período, procedencia).
  Un lector que no filtra procedencia en A tampoco lo hace en B; uno que sí
  (FactsService) usa la columna ``provenance`` de la proyección.
- **El total de ventas y el costo siguen saliendo de ``SaleEntry``.** Los
  tenders sólo reparten ese total entre medios de pago, liquidez y deuda.

Una operación anulada (``VOIDED``) no aporta en ninguna población: sus ventas
quedan con ``voided_at`` y sus tenders se filtran por el estado de la cabecera.

Una operación cuyas líneas tienen procedencias distintas no existe hoy (el
writer de la caja crea todas REAL) y **no se le inventa una**: queda fuera de B y
``operaciones_mixtas_con_procedencia_mezclada()`` la cuenta, para que un test o
un diagnóstico lo vean en vez de que la plata desaparezca en silencio.
"""

import uuid
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.sql import ColumnElement, Select, Subquery

from app.domain.transaction import MIXED_PAYMENT_METHOD
from app.persistence.models.pos_operation import PosOperation, PosOperationLine, PosTender
from app.persistence.models.transaction import SaleEntry

#: Una operación anulada conserva sus tenders para reimprimir y auditar, pero no
#: aporta plata a ningún agregado.
ESTADO_OPERACION_VIVA = "COMPLETED"


def venta_no_mixta() -> ColumnElement[bool]:
    """Predicado WHERE de la población A: la línea lleva su propio método.

    ``payment_method`` es NOT NULL, así que un ``!=`` no descarta nulos.
    """
    return SaleEntry.payment_method != MIXED_PAYMENT_METHOD


def _operaciones_mixtas(tenant_id: uuid.UUID) -> Select[tuple[uuid.UUID]]:
    """Ids de las operaciones con más de un método distinto. Se define UNA vez."""
    return (
        select(PosTender.operation_id)
        .where(PosTender.tenant_id == tenant_id)
        .group_by(PosTender.operation_id)
        .having(func.count(func.distinct(PosTender.payment_method)) > 1)
    )


def _procedencia_por_operacion(tenant_id: uuid.UUID) -> Subquery:
    """Una fila por operación con la procedencia mínima y máxima de sus líneas."""
    return (
        select(
            PosOperationLine.operation_id.label("operation_id"),
            func.min(SaleEntry.provenance).label("prov_min"),
            func.max(SaleEntry.provenance).label("prov_max"),
        )
        .join(SaleEntry, SaleEntry.id == PosOperationLine.sale_entry_id)
        .where(
            PosOperationLine.tenant_id == tenant_id,
            SaleEntry.tenant_id == tenant_id,
        )
        .group_by(PosOperationLine.operation_id)
        .subquery("pos_prov")
    )


def tenders_mixtos(
    tenant_id: uuid.UUID,
    desde: date | None = None,
    hasta: date | None = None,
) -> Subquery:
    """Población B: una fila por tender de cada operación mixta viva del tenant.

    Columnas: ``operation_id``, ``sale_date`` (la ``operation_date`` de la
    cabecera, datetime), ``customer_id``, ``payment_method``, ``amount_ars``,
    ``provenance``. Los límites, si vienen, se comparan por ``func.date`` igual
    que las ventas.
    """
    prov = _procedencia_por_operacion(tenant_id)
    q = (
        select(
            PosTender.operation_id.label("operation_id"),
            PosOperation.operation_date.label("sale_date"),
            PosOperation.customer_id.label("customer_id"),
            PosTender.payment_method.label("payment_method"),
            PosTender.amount_ars.label("amount_ars"),
            prov.c.prov_min.label("provenance"),
        )
        .join(
            PosOperation,
            (PosOperation.id == PosTender.operation_id)
            & (PosOperation.tenant_id == tenant_id),
        )
        .join(prov, prov.c.operation_id == PosTender.operation_id)
        .where(
            PosTender.tenant_id == tenant_id,
            PosOperation.status == ESTADO_OPERACION_VIVA,
            PosTender.operation_id.in_(_operaciones_mixtas(tenant_id)),
            prov.c.prov_min == prov.c.prov_max,
        )
    )
    if desde is not None:
        q = q.where(func.date(PosOperation.operation_date) >= desde)
    if hasta is not None:
        q = q.where(func.date(PosOperation.operation_date) <= hasta)
    return q.subquery("tenders_mixtos")


def operaciones_mixtas_con_procedencia_mezclada(tenant_id: uuid.UUID) -> Select[tuple[int]]:
    """Cuántas operaciones mixtas vivas quedan fuera de B por mezclar REAL y DEMO."""
    prov = _procedencia_por_operacion(tenant_id)
    return (
        select(func.count())
        .select_from(prov)
        .join(
            PosOperation,
            (PosOperation.id == prov.c.operation_id) & (PosOperation.tenant_id == tenant_id),
        )
        .where(
            PosOperation.status == ESTADO_OPERACION_VIVA,
            prov.c.operation_id.in_(_operaciones_mixtas(tenant_id)),
            prov.c.prov_min != prov.c.prov_max,
        )
    )
