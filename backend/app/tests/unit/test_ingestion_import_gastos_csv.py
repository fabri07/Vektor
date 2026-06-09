"""Test end-to-end del import single-sheet de un CSV de gastos operativos.

Reproduce el archivo real que motivó la remediación: columnas
`fecha,concepto,categoria,monto,forma_pago,tipo,recurrente` con categorías
libres es-AR. Verifica que SIN mapeo manual:
  - todas las filas se insertan;
  - la categoría se normaliza al catálogo canónico (no queda "importado");
  - `categoria` gana sobre `tipo` (prioridad de keywords);
  - forma_pago y recurrente se preservan (antes: hardcode transfer / False);
  - check_nonempty_import corta cuando no se insertó nada.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.ingestion_import_service import (
    EmptyImportError,
    check_nonempty_import,
    insert_confirmed_data,
)
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import ExpenseEntry


def _gastos_summary() -> dict[str, Any]:
    rows = [
        {
            "fecha": "2026-03-09",
            "concepto": "Seguro comercio",
            "categoria": "Seguros",
            "monto": "49490",
            "forma_pago": "debito_automatico",
            "tipo": "fijo",
            "recurrente": "True",
        },
        {
            "fecha": "2026-03-10",
            "concepto": "Comisión MercadoPago",
            "categoria": "Bancarios",
            "monto": "53747",
            "forma_pago": "debito",
            "tipo": "variable",
            "recurrente": "False",
        },
        {
            "fecha": "2026-03-13",
            "concepto": "Combustible (delivery)",
            "categoria": "Logística",
            "monto": "32997",
            "forma_pago": "efectivo",
            "tipo": "variable",
            "recurrente": "False",
        },
        {
            "fecha": "2026-04-06",
            "concepto": "Alquiler local",
            "categoria": "Alquiler",
            "monto": "879961",
            "forma_pago": "debito_automatico",
            "tipo": "fijo",
            "recurrente": "True",
        },
    ]
    return {
        "file_type": "spreadsheet",
        "inferred_type": "gastos",
        "row_count": len(rows),
        "gastos_detectados": rows,
        "ventas_detectadas": [],
        "stock_detectado": [],
    }


@pytest.mark.asyncio
async def test_import_gastos_csv_sin_mapeo_manual(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    counts = await insert_confirmed_data(
        db_session,
        sample_tenant.tenant_id,
        _gastos_summary(),
        {"gastos": True},
    )
    assert counts["gastos"] == 4

    expenses = (
        (await db_session.execute(select(ExpenseEntry).order_by(ExpenseEntry.transaction_date)))
        .scalars()
        .all()
    )
    by_desc = {e.description: e for e in expenses}

    seguro = by_desc["Seguro comercio"]
    assert seguro.category == "INSURANCE"  # "Seguros", no "importado" ni "fijo"
    assert seguro.payment_method == "transfer"  # debito_automatico → transfer
    assert seguro.is_recurring is True
    assert seguro.amount == Decimal("49490")

    comision = by_desc["Comisión MercadoPago"]
    assert comision.category == "BANK_FEES"
    assert comision.payment_method == "debit_card"
    assert comision.is_recurring is False

    combustible = by_desc["Combustible (delivery)"]
    assert combustible.category == "LOGISTICS"
    assert combustible.payment_method == "cash"

    alquiler = by_desc["Alquiler local"]
    assert alquiler.category == "RENT"
    assert alquiler.transaction_date.date().isoformat() == "2026-04-06"


@pytest.mark.asyncio
async def test_import_gastos_categoria_desconocida_preserva_label(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    summary = _gastos_summary()
    summary["gastos_detectados"] = [
        {
            "fecha": "2026-05-01",
            "concepto": "Cuota club",
            "categoria": "Donaciones del barrio",
            "monto": "9000",
            "forma_pago": "efectivo",
            "tipo": "variable",
            "recurrente": "False",
        }
    ]
    summary["row_count"] = 1
    counts = await insert_confirmed_data(
        db_session, sample_tenant.tenant_id, summary, {"gastos": True}
    )
    assert counts["gastos"] == 1
    expense = (await db_session.execute(select(ExpenseEntry))).scalar_one()
    assert expense.category == "OTHER"
    assert (expense.custom_fields or {}).get("category_label") == "Donaciones del barrio"


def test_check_nonempty_import_corta_con_datos_y_cero_insertados() -> None:
    with pytest.raises(EmptyImportError):
        check_nonempty_import(
            {"ventas": 0, "gastos": 0, "productos": 0},
            {"row_count": 81, "gastos_detectados": [{"x": "1"}]},
            {"gastos": True},
        )


def test_check_nonempty_import_no_corta_sin_confirmacion_ni_datos() -> None:
    # Sin filas en el archivo → no corta.
    check_nonempty_import(
        {"ventas": 0, "gastos": 0, "productos": 0}, {"row_count": 0}, {"gastos": True}
    )
    # Con filas pero sin nada confirmado → no corta.
    check_nonempty_import(
        {"ventas": 0, "gastos": 0, "productos": 0},
        {"row_count": 10, "gastos_detectados": [{"x": "1"}]},
        {},
    )
    # Con inserciones → no corta.
    check_nonempty_import(
        {"ventas": 0, "gastos": 5, "productos": 0},
        {"row_count": 10, "gastos_detectados": [{"x": "1"}]},
        {"gastos": True},
    )
