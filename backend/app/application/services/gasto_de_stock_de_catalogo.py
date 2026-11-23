"""Gasto de mercadería (COGS) de un stock de catálogo tratado como COMPRA.

Lo escribe la importación (``ingestion_import_service._add_catalog_initial_stock_cogs``)
cuando el usuario marcó el stock de la hoja como «La compré ahora» y la fila trae
costo, y lo escribe la mejora ``compra_catalogo_sin_costo`` cuando completa una
compra que quedó sin costo. Vive acá, aparte de la importación, para que las dos
armen EXACTAMENTE el mismo gasto y para que una mejora no importe un módulo de
12.000 líneas.

Regla contable: una compra de mercadería es un gasto ``INVENTORY``/COGS ligado al
producto, con la misma fila de origen que el movimiento (así una relectura no lo
duplica y borrar el archivo lo anula). No se agrega a la sesión: lo hace el caller.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Final

from app.domain.expense_categories import infer_expense_type
from app.persistence.models.transaction import ExpenseEntry

#: Medio de pago cuando el archivo no dice cómo se pagó: el default histórico de la
#: importación de catálogo (banco, no efectivo físico).
MEDIO_DE_PAGO_POR_DEFECTO: Final = "transfer"


def armar_gasto_de_stock_de_catalogo(
    tenant_id: uuid.UUID,
    *,
    product_id: uuid.UUID,
    product_name: str,
    amount: Decimal,
    tx_date: datetime,
    source_upload_id: uuid.UUID | None,
    source_row_ref: str | None,
    payment_method: str = MEDIO_DE_PAGO_POR_DEFECTO,
) -> ExpenseEntry:
    """El ``ExpenseEntry`` «Stock inicial: X» de una compra de catálogo."""
    expense = ExpenseEntry(
        tenant_id=tenant_id,
        amount=amount,
        category="INVENTORY",
        # Producto ligado + categoría INVENTORY ⇒ COGS (mismo helper que el resto).
        expense_type=infer_expense_type("INVENTORY", product_id=product_id),
        transaction_date=tx_date,
        description=f"Stock inicial: {product_name}"[:500],
        is_recurring=False,
        payment_method=payment_method,
        provenance="REAL",
        product_id=product_id,
        source_upload_id=source_upload_id,
    )
    if source_row_ref is not None:
        expense.source_row_ref = source_row_ref
    return expense
