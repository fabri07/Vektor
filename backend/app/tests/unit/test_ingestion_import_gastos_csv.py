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
from app.persistence.models.unclassified_record import UnclassifiedRecord


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


@pytest.mark.asyncio
async def test_reimport_same_file_is_idempotent(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """B1: re-importar el mismo archivo (mismo uploaded_file_id) = 0 filas nuevas.

    El primer import inserta las 4 filas; el segundo, con el mismo
    uploaded_file_id, las saltea todas por la huella de fila.
    """
    import uuid as _uuid

    from app.persistence.models.file import UploadedFile

    uploaded = UploadedFile(
        id=_uuid.uuid4(),
        tenant_id=sample_tenant.tenant_id,
        original_filename="gastos.csv",
        s3_key="test/gastos.csv",
        content_type="text/csv",
        size_bytes=512,
        purpose="ingestion",
    )
    db_session.add(uploaded)
    await db_session.flush()

    first = await insert_confirmed_data(
        db_session,
        sample_tenant.tenant_id,
        _gastos_summary(),
        {"gastos": True},
        uploaded_file_id=uploaded.id,
    )
    assert first["gastos"] == 4

    second = await insert_confirmed_data(
        db_session,
        sample_tenant.tenant_id,
        _gastos_summary(),
        {"gastos": True},
        uploaded_file_id=uploaded.id,
    )
    assert second["gastos"] == 0  # idempotente: nada nuevo

    total = (
        (await db_session.execute(select(ExpenseEntry))).scalars().all()
    )
    assert len(total) == 4
    # source_upload_id se pobló en todas.
    assert all(e.source_upload_id == uploaded.id for e in total)


@pytest.mark.asyncio
async def test_import_without_uploaded_file_id_not_deduped(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Sin uploaded_file_id (ej. correctivo de repair) NO se dedupea por huella."""
    first = await insert_confirmed_data(
        db_session, sample_tenant.tenant_id, _gastos_summary(), {"gastos": True}
    )
    second = await insert_confirmed_data(
        db_session, sample_tenant.tenant_id, _gastos_summary(), {"gastos": True}
    )
    assert first["gastos"] == 4
    assert second["gastos"] == 4  # sin huella, ambos insertan


@pytest.mark.asyncio
async def test_fila_sin_monto_va_a_otros_y_no_se_reimporta(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """F-H4: una fila sin monto va a "Otros" y su huella queda registrada.

    **Esto reemplaza al contrato B1 anterior** ("una fila sin monto no se quema,
    así el archivo corregido la importa"), que existía porque hasta F-H4 la fila
    desaparecía en silencio: no quedaba en ningún lado, y la única vía de rescate
    era volver a subir el archivo. Ahora la fila se captura con el motivo, así que
    el rescate es completarla desde "Otros" — y la huella tiene que registrarse,
    porque una captura es output PERSISTIDO y sin huella re-subir el archivo
    crearía un segundo `UnclassifiedRecord` para la misma fila.

    Es el mismo criterio que ya rige para una fila con fecha ilegible (F6-A2):
    capturar y registrar. La consecuencia —el archivo corregido no la re-importa—
    es explícita y elegida, no un efecto colateral.

    Lo que este test sigue protegiendo de B1: la fila que SÍ entró no se duplica
    en ningún re-import.
    """
    import uuid as _uuid

    from app.persistence.models.file import UploadedFile

    uploaded = UploadedFile(
        id=_uuid.uuid4(),
        tenant_id=sample_tenant.tenant_id,
        original_filename="gastos_parciales.csv",
        s3_key="test/gastos_parciales.csv",
        content_type="text/csv",
        size_bytes=256,
        purpose="ingestion",
    )
    db_session.add(uploaded)
    await db_session.flush()

    def _summary(monto_fila0: str) -> dict[str, Any]:
        # index 0: monto variable (vacío = inválida); index 1: monto válido fijo.
        return {
            "file_type": "spreadsheet",
            "inferred_type": "gastos",
            "row_count": 2,
            "gastos_detectados": [
                {
                    "fecha": "2026-03-09",
                    "concepto": "Seguro comercio",
                    "categoria": "Seguros",
                    "monto": monto_fila0,
                    "forma_pago": "efectivo",
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
            ],
            "ventas_detectadas": [],
            "stock_detectado": [],
        }

    # 1er import: fila 0 SIN monto → "Otros" con el motivo; fila 1 → gasto.
    first = await insert_confirmed_data(
        db_session,
        sample_tenant.tenant_id,
        _summary(""),
        {"gastos": True},
        uploaded_file_id=uploaded.id,
    )
    assert first["gastos"] == 1
    assert first["filas_sin_monto"] == 1
    assert first["otros"] == 1
    expenses = (await db_session.execute(select(ExpenseEntry))).scalars().all()
    assert {e.description for e in expenses} == {"Comisión MercadoPago"}

    otros = (await db_session.execute(select(UnclassifiedRecord))).scalars().all()
    assert len(otros) == 1
    assert "sin monto" in (otros[0].context_label or "").lower()
    # El motivo dice cómo salir, no sólo qué pasó.
    assert "precio unitario" in (otros[0].context_label or "")

    # 2do import del MISMO archivo, fila 0 AHORA con monto válido. La captura ya
    # es output persistido: la fila no se re-importa (se completa desde "Otros") y
    # —lo que importa— tampoco se duplica en la bandeja.
    second = await insert_confirmed_data(
        db_session,
        sample_tenant.tenant_id,
        _summary("49490"),
        {"gastos": True},
        uploaded_file_id=uploaded.id,
    )
    assert second["gastos"] == 0
    assert second["otros"] == 0

    expenses = (await db_session.execute(select(ExpenseEntry))).scalars().all()
    assert {e.description for e in expenses} == {"Comisión MercadoPago"}
    assert len((await db_session.execute(select(UnclassifiedRecord))).scalars().all()) == 1

    # 3er import idéntico → sigue idempotente por los dos lados.
    third = await insert_confirmed_data(
        db_session,
        sample_tenant.tenant_id,
        _summary("49490"),
        {"gastos": True},
        uploaded_file_id=uploaded.id,
    )
    assert third["gastos"] == 0
    assert len((await db_session.execute(select(ExpenseEntry))).scalars().all()) == 1
    assert len((await db_session.execute(select(UnclassifiedRecord))).scalars().all()) == 1


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
