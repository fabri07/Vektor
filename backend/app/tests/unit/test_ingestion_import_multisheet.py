"""Tests del path por contexto de _insert_multisheet_data (Fase 1 — mapeo por hoja).

Verifica que en archivos multi-contexto:
  - el mapeo explícito por contexto (context_mappings) se aplica;
  - la inclusión por contexto (context_confirmed) se respeta;
  - sin mapeo, una columna no reconocida por keyword se descarta (prueba que el
    mapeo importa);
  - los custom_field:{key} por contexto se persisten en custom_fields.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.ingestion_import_service import (
    _parse_date,
    insert_confirmed_data,
)
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import ExpenseEntry, SaleEntry
from app.persistence.models.unclassified_record import UnclassifiedRecord


def test_parse_date_handles_datetime_and_date_formats() -> None:
    """_parse_date conserva la hora cuando el archivo la trae; si solo hay fecha,
    queda a medianoche."""
    from datetime import datetime

    assert _parse_date("2026-06-05T14:30:00") == datetime(2026, 6, 5, 14, 30, 0)
    assert _parse_date("2026-06-05 14:30:00") == datetime(2026, 6, 5, 14, 30, 0)
    assert _parse_date("05/06/2026 14:30") == datetime(2026, 6, 5, 14, 30, 0)
    # Solo fecha → medianoche.
    assert _parse_date("2026-06-05") == datetime(2026, 6, 5, 0, 0, 0)
    assert _parse_date("05/06/2026") == datetime(2026, 6, 5, 0, 0, 0)
    assert _parse_date("basura") is None
    assert _parse_date(None) is None


def _multisheet_summary() -> dict[str, Any]:
    """Summary multi-contexto: 'valor' (no keyword) en ventas, 'monto' (keyword) en gastos."""
    return {
        "file_type": "spreadsheet",
        "inferred_type": "mixed",
        "multi_sheet": True,
        "mapping_contexts": [
            {
                "context_id": "sheet:Ventas",
                "entity_type": "sale",
                "source_kind": "sheet",
                "headers": ["fecha", "valor", "vendedor"],
                "fields": None,
                "preview_rows": [],
                "row_count": 1,
            },
            {
                "context_id": "sheet:Gastos",
                "entity_type": "expense",
                "source_kind": "sheet",
                "headers": ["fecha", "monto"],
                "fields": None,
                "preview_rows": [],
                "row_count": 1,
            },
        ],
        "ventas_detectadas": [
            {
                "fecha": "2024-01-15",
                "valor": "5400",
                "vendedor": "Juan",
                "__context__": "sheet:Ventas",
            }
        ],
        "gastos_detectados": [
            {"fecha": "2024-01-15", "monto": "12000", "__context__": "sheet:Gastos"}
        ],
        "stock_detectado": [],
    }


async def test_context_mapping_applied_and_keyword_fallback(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """El mapeo explícito hace entrar 'valor'→amount; gastos entra por keyword 'monto'."""
    counts = await insert_confirmed_data(
        db_session,
        sample_tenant.tenant_id,
        _multisheet_summary(),
        {"ventas": True, "gastos": True},
        context_mappings={
            "sheet:Ventas": {"valor": "amount", "fecha": "transaction_date"}
        },
        context_confirmed={"sheet:Ventas": True, "sheet:Gastos": True},
    )
    assert counts["ventas"] == 1
    assert counts["gastos"] == 1

    sale = (await db_session.execute(select(SaleEntry))).scalar_one()
    assert sale.amount == Decimal("5400")
    expense = (await db_session.execute(select(ExpenseEntry))).scalar_one()
    assert expense.amount == Decimal("12000")


async def test_context_confirmed_excludes_context(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """context_confirmed=False para una hoja → sus filas no se importan."""
    counts = await insert_confirmed_data(
        db_session,
        sample_tenant.tenant_id,
        _multisheet_summary(),
        {"ventas": True, "gastos": True},
        context_mappings={"sheet:Ventas": {"valor": "amount"}},
        context_confirmed={"sheet:Ventas": True, "sheet:Gastos": False},
    )
    assert counts["ventas"] == 1
    assert counts["gastos"] == 0
    assert (await db_session.execute(select(ExpenseEntry))).first() is None


async def test_unmapped_unknown_column_is_skipped(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Sin mapeo, 'valor' no es keyword conocida → la venta se descarta (el mapeo importa)."""
    counts = await insert_confirmed_data(
        db_session,
        sample_tenant.tenant_id,
        _multisheet_summary(),
        {"ventas": True},
        context_confirmed={"sheet:Ventas": True},
    )
    assert counts["ventas"] == 0


async def test_context_custom_field_persisted(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Un mapeo custom_field:{key} por contexto persiste el valor en custom_fields."""
    counts = await insert_confirmed_data(
        db_session,
        sample_tenant.tenant_id,
        _multisheet_summary(),
        {"ventas": True},
        context_mappings={
            "sheet:Ventas": {
                "valor": "amount",
                "vendedor": "custom_field:vendedor",
            }
        },
        context_confirmed={"sheet:Ventas": True},
    )
    assert counts["ventas"] == 1
    sale = (await db_session.execute(select(SaleEntry))).scalar_one()
    # La igualdad es exacta a propósito: lo que se guarda acá es lo que después lee
    # el resto del sistema, así que una clave nueva tiene que pasar por este test.
    # Además del valor mapeado van dos marcas internas: la traza de resolución de
    # cliente por fila (F7c — "anonymous" porque la hoja no mapea ninguna columna
    # customer_*) y la hoja de origen (F-H3.d.2, lo que permite aplicar el replay
    # por hoja en vez de al archivo entero).
    assert sale.custom_fields == {
        "vendedor": "Juan",
        "_customer_resolution": "anonymous",
        "_import_context": "sheet:Ventas",
    }
    # El marcador interno nunca se filtra a custom_fields.
    assert "__context__" not in sale.custom_fields


async def test_multisheet_reimport_is_idempotent(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """B1: re-importar el mismo archivo multi-hoja = 0 filas nuevas."""
    import uuid as _uuid

    from app.persistence.models.file import UploadedFile

    uploaded = UploadedFile(
        id=_uuid.uuid4(),
        tenant_id=sample_tenant.tenant_id,
        original_filename="mixto.xlsx",
        s3_key="test/mixto.xlsx",
        content_type="application/vnd.ms-excel",
        size_bytes=2048,
        purpose="ingestion",
    )
    db_session.add(uploaded)
    await db_session.flush()

    kwargs: dict[str, Any] = {
        "context_mappings": {"sheet:Ventas": {"valor": "amount", "fecha": "transaction_date"}},
        "context_confirmed": {"sheet:Ventas": True, "sheet:Gastos": True},
        "uploaded_file_id": uploaded.id,
    }
    confirmed = {"ventas": True, "gastos": True}
    first = await insert_confirmed_data(
        db_session, sample_tenant.tenant_id, _multisheet_summary(), confirmed, **kwargs
    )
    assert first["ventas"] == 1
    assert first["gastos"] == 1

    second = await insert_confirmed_data(
        db_session, sample_tenant.tenant_id, _multisheet_summary(), confirmed, **kwargs
    )
    assert second["ventas"] == 0
    assert second["gastos"] == 0

    sales = (await db_session.execute(select(SaleEntry))).scalars().all()
    expenses = (await db_session.execute(select(ExpenseEntry))).scalars().all()
    assert len(sales) == 1
    assert len(expenses) == 1
    assert sales[0].source_upload_id == uploaded.id
    assert expenses[0].source_upload_id == uploaded.id


async def test_multisheet_invalid_row_not_burned_reimport_corrected(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """B1 (registración diferida) en el path por contexto.

    Una fila de gastos sin monto no inserta nada y NO se quema. Al re-subir el
    MISMO archivo con esa fila corregida, se importa (no quedó marcada); la fila
    que ya había entrado sigue idempotente.
    """
    import uuid as _uuid

    from app.persistence.models.file import UploadedFile

    uploaded = UploadedFile(
        id=_uuid.uuid4(),
        tenant_id=sample_tenant.tenant_id,
        original_filename="mixto_parcial.xlsx",
        s3_key="test/mixto_parcial.xlsx",
        content_type="application/vnd.ms-excel",
        size_bytes=2048,
        purpose="ingestion",
    )
    db_session.add(uploaded)
    await db_session.flush()

    def _summary(monto_fila0: str) -> dict[str, Any]:
        # Hoja de gastos con 2 filas: index 0 (monto variable) + index 1 (válida).
        return {
            "file_type": "spreadsheet",
            "inferred_type": "expense",
            "multi_sheet": True,
            "mapping_contexts": [
                {
                    "context_id": "sheet:Gastos",
                    "entity_type": "expense",
                    "source_kind": "sheet",
                    "headers": ["fecha", "concepto", "monto"],
                    "fields": None,
                    "preview_rows": [],
                    "row_count": 2,
                }
            ],
            "ventas_detectadas": [],
            "gastos_detectados": [
                {
                    "fecha": "2024-01-15",
                    "concepto": "Luz",
                    "monto": monto_fila0,
                    "__context__": "sheet:Gastos",
                },
                {
                    "fecha": "2024-01-16",
                    "concepto": "Agua",
                    "monto": "8000",
                    "__context__": "sheet:Gastos",
                },
            ],
            "stock_detectado": [],
        }

    kwargs: dict[str, Any] = {
        "context_confirmed": {"sheet:Gastos": True},
        "uploaded_file_id": uploaded.id,
    }

    # 1er import: fila 0 sin monto → no inserta; fila 1 → inserta.
    first = await insert_confirmed_data(
        db_session, sample_tenant.tenant_id, _summary(""), {"gastos": True}, **kwargs
    )
    assert first["gastos"] == 1
    descs = {
        e.description
        for e in (await db_session.execute(select(ExpenseEntry))).scalars().all()
    }
    assert descs == {"Agua"}

    # 2do import del MISMO archivo, fila 0 ahora con monto → entra (no quemada);
    # fila 1 ya estaba → idempotente.
    second = await insert_confirmed_data(
        db_session, sample_tenant.tenant_id, _summary("4500"), {"gastos": True}, **kwargs
    )
    assert second["gastos"] == 1
    descs = {
        e.description
        for e in (await db_session.execute(select(ExpenseEntry))).scalars().all()
    }
    assert descs == {"Luz", "Agua"}

    # 3er import idéntico → 0 nuevas.
    third = await insert_confirmed_data(
        db_session, sample_tenant.tenant_id, _summary("4500"), {"gastos": True}, **kwargs
    )
    assert third["gastos"] == 0
    assert len((await db_session.execute(select(ExpenseEntry))).scalars().all()) == 2


def _text_summary() -> dict[str, Any]:
    """Summary de documento de texto con dos grupos detectados (ventas + gastos)."""
    return {
        "file_type": "text",
        "mapping_contexts": [
            {
                "context_id": "text:sale",
                "entity_type": "sale",
                "source_kind": "text_group",
                "headers": None,
                "fields": ["linea"],
                "preview_rows": [{"linea": "Venta 5000"}],
                "row_count": 1,
            },
            {
                "context_id": "text:expense",
                "entity_type": "expense",
                "source_kind": "text_group",
                "headers": None,
                "fields": ["linea"],
                "preview_rows": [{"linea": "Pago luz 3000"}],
                "row_count": 1,
            },
        ],
        "ventas_detectadas": [
            {"linea": "Venta 5000", "montos": ["5000"], "__context__": "text:sale"}
        ],
        "gastos_detectados": [
            {"linea": "Pago luz 3000", "montos": ["3000"], "__context__": "text:expense"}
        ],
        "stock_detectado": [],
    }


async def test_multisheet_venta_sin_fecha_va_a_otros_no_duplica_al_reimportar(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """F6-A2/A3: una venta con monto válido pero sin fecha reconocible va a /otros
    (no se inventa "hoy"); la captura es output persistido, así re-subir el MISMO
    archivo no re-crea el UnclassifiedRecord (idempotencia por fingerprint)."""
    import uuid as _uuid

    from app.persistence.models.file import UploadedFile

    uploaded = UploadedFile(
        id=_uuid.uuid4(),
        tenant_id=sample_tenant.tenant_id,
        original_filename="ventas_sin_fecha.xlsx",
        s3_key="test/ventas_sin_fecha.xlsx",
        content_type="application/vnd.ms-excel",
        size_bytes=1024,
        purpose="ingestion",
    )
    db_session.add(uploaded)
    await db_session.flush()

    summary: dict[str, Any] = {
        "file_type": "spreadsheet",
        "inferred_type": "ventas",
        "multi_sheet": True,
        "mapping_contexts": [
            {
                "context_id": "sheet:Ventas",
                "entity_type": "sale",
                "source_kind": "sheet",
                "headers": ["detalle", "valor"],
                "row_count": 1,
            }
        ],
        "ventas_detectadas": [
            {"detalle": "Venta mostrador", "valor": "5000", "__context__": "sheet:Ventas"}
        ],
        "gastos_detectados": [],
        "stock_detectado": [],
    }
    kwargs: dict[str, Any] = {
        "context_confirmed": {"sheet:Ventas": True},
        "context_mappings": {"sheet:Ventas": {"valor": "amount"}},
        "uploaded_file_id": uploaded.id,
    }

    first = await insert_confirmed_data(
        db_session, sample_tenant.tenant_id, summary, {"ventas": True}, **kwargs
    )
    assert first["ventas"] == 0
    assert first["otros"] == 1
    assert (await db_session.execute(select(SaleEntry))).scalars().all() == []
    record = (await db_session.execute(select(UnclassifiedRecord))).scalar_one()
    assert record.suggested_entity == "sale"

    # Re-import del mismo archivo → no duplica el /otros.
    second = await insert_confirmed_data(
        db_session, sample_tenant.tenant_id, summary, {"ventas": True}, **kwargs
    )
    assert second["otros"] == 0
    assert len((await db_session.execute(select(UnclassifiedRecord))).scalars().all()) == 1


async def test_multisheet_venta_con_fecha_valida_conserva_la_fecha(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """F6-A (mutation guard): una fecha válida NUNCA se reemplaza por hoy ni se
    rutea a /otros — se registra la venta con la fecha del archivo."""
    from datetime import date as _date

    summary: dict[str, Any] = {
        "file_type": "spreadsheet",
        "inferred_type": "ventas",
        "multi_sheet": True,
        "mapping_contexts": [
            {
                "context_id": "sheet:Ventas",
                "entity_type": "sale",
                "source_kind": "sheet",
                "headers": ["fecha", "valor"],
                "row_count": 1,
            }
        ],
        "ventas_detectadas": [
            {"fecha": "2024-03-05", "valor": "5000", "__context__": "sheet:Ventas"}
        ],
        "gastos_detectados": [],
        "stock_detectado": [],
    }
    counts = await insert_confirmed_data(
        db_session,
        sample_tenant.tenant_id,
        summary,
        {"ventas": True},
        context_confirmed={"sheet:Ventas": True},
        context_mappings={"sheet:Ventas": {"valor": "amount", "fecha": "transaction_date"}},
    )
    assert counts["ventas"] == 1
    assert counts["otros"] == 0
    sale = (await db_session.execute(select(SaleEntry))).scalar_one()
    assert sale.transaction_date.date() == _date(2024, 3, 5)


async def test_text_contexts_van_a_otros_sin_inventar_fecha(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """F6-A4: los documentos de texto/imagen no extraen fecha. En vez de estampillar
    "hoy" (invariante 2d), cada línea con monto va a /otros para revisión manual.
    No se crea ninguna venta ni gasto; la lectura real con fecha es F7."""
    counts = await insert_confirmed_data(
        db_session,
        sample_tenant.tenant_id,
        _text_summary(),
        {},
        context_confirmed={"text:sale": True, "text:expense": True},
    )
    assert counts["ventas"] == 0
    assert counts["gastos"] == 0
    assert counts["otros"] == 2
    assert (await db_session.execute(select(SaleEntry))).scalars().all() == []
    assert (await db_session.execute(select(ExpenseEntry))).scalars().all() == []
    records = (await db_session.execute(select(UnclassifiedRecord))).scalars().all()
    assert {r.suggested_entity for r in records} == {"sale", "expense"}


async def test_text_context_entity_override_va_a_otros_con_sugerencia(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """context_entity reasigna el grupo 'ventas' a gasto: la línea va a /otros
    sugerida como gasto (no se inventa la fecha — F6-A4)."""
    counts = await insert_confirmed_data(
        db_session,
        sample_tenant.tenant_id,
        _text_summary(),
        {},
        context_confirmed={"text:sale": True, "text:expense": False},
        context_entity={"text:sale": "expense"},
    )
    assert counts["ventas"] == 0
    assert counts["gastos"] == 0
    assert counts["otros"] == 1
    record = (await db_session.execute(select(UnclassifiedRecord))).scalar_one()
    assert record.suggested_entity == "expense"
