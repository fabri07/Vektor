"""Bulk import de la bandeja "Otros" (`bulk_import_unclassified`).

Importa en lote los PENDING sugeridos como venta/gasto con las mismas
normalizaciones canónicas que el import de archivos; lo no parseable queda
PENDING y los sugeridos como producto no entran.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.ingestion_import_service import bulk_import_unclassified
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import ExpenseEntry, SaleEntry
from app.persistence.models.unclassified_record import UnclassifiedRecord


def _record(
    tenant_id: Any, row: dict[str, str], entity: str | None
) -> UnclassifiedRecord:
    return UnclassifiedRecord(
        tenant_id=tenant_id,
        source="reanalysis",
        context_label="Reanálisis: ventas",
        row_data=row,
        suggested_entity=entity,
    )


@pytest.mark.asyncio
async def test_bulk_import_sales_and_expenses(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    tid = sample_tenant.tenant_id
    db_session.add_all(
        [
            _record(
                tid,
                {"fecha": "2026-05-01", "detalle": "Venta del día", "monto": "120000"},
                "sale",
            ),
            _record(
                tid,
                {
                    "fecha": "2026-05-02",
                    "detalle": "Compra fiada almacén",
                    "monto": "30000",
                    "categoria": "Mercadería",
                    "forma_pago": "cuenta corriente",
                },
                "expense",
            ),
            # Sin monto parseable → queda PENDING.
            _record(tid, {"fecha": "2026-05-03", "detalle": "Sin monto"}, "sale"),
            # Sugerido producto → nunca entra al bulk.
            _record(tid, {"nombre": "Velas", "precio": "500"}, "product"),
        ]
    )
    await db_session.flush()

    counts = await bulk_import_unclassified(db_session, tid)
    assert counts == {"imported_sales": 1, "imported_expenses": 1, "skipped": 1}

    sale = (await db_session.execute(select(SaleEntry))).scalar_one()
    assert sale.amount == Decimal("120000")
    assert sale.payment_method == "cash"
    assert sale.notes == "Venta del día"

    expense = (await db_session.execute(select(ExpenseEntry))).scalar_one()
    assert expense.category == "INVENTORY"
    assert expense.expense_type == "COGS"
    assert expense.payment_method == "account"

    records = (await db_session.execute(select(UnclassifiedRecord))).scalars().all()
    by_status: dict[str, int] = {}
    for r in records:
        by_status[r.status] = by_status.get(r.status, 0) + 1
    # 2 importados; el sin-monto y el producto siguen PENDING.
    assert by_status == {"IMPORTED": 2, "PENDING": 2}


@pytest.mark.asyncio
async def test_bulk_import_entity_filter(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    tid = sample_tenant.tenant_id
    db_session.add_all(
        [
            _record(tid, {"fecha": "2026-05-01", "monto": "1000"}, "sale"),
            _record(tid, {"fecha": "2026-05-01", "monto": "2000"}, "expense"),
        ]
    )
    await db_session.flush()

    counts = await bulk_import_unclassified(db_session, tid, entity_filter="sale")
    assert counts == {"imported_sales": 1, "imported_expenses": 0, "skipped": 0}
    # El gasto sugerido sigue pendiente.
    pending = (
        (
            await db_session.execute(
                select(UnclassifiedRecord).where(UnclassifiedRecord.status == "PENDING")
            )
        )
        .scalars()
        .all()
    )
    assert len(pending) == 1
    assert pending[0].suggested_entity == "expense"
