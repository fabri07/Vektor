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


async def test_multisheet_gasto_sin_monto_va_a_otros_y_no_se_duplica(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """E7a-lite: el gasto sin monto va a "Otros", y la captura es idempotente.

    Antes esta fila **desaparecía sin rastro** en este camino (``_add_expense``
    devolvía ``False`` sin capturar) mientras el de tabla suelta sí la capturaba:
    el mismo archivo perdía filas o no según cómo estuviera armado. Ahora hace lo
    mismo que ``_add_sale`` — va a "Otros" con su motivo, y la huella se quema
    justamente porque el ``UnclassifiedRecord`` ES output persistido: sin quemarla,
    cada reimport del archivo crearía otra copia de la misma fila en "Otros".

    La vía de recuperación es clasificarla desde "Otros" (o la relectura, que
    libera las refs de las filas capturadas), no reimportar el archivo.
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

    from app.persistence.models.unclassified_record import UnclassifiedRecord

    async def _otros() -> list[UnclassifiedRecord]:
        res = await db_session.execute(select(UnclassifiedRecord))
        return list(res.scalars().all())

    # 1er import: fila 0 sin monto → "Otros" con el motivo; fila 1 → gasto.
    first = await insert_confirmed_data(
        db_session, sample_tenant.tenant_id, _summary(""), {"gastos": True}, **kwargs
    )
    assert first["gastos"] == 1
    assert first["otros"] == 1
    assert first["filas_sin_monto"] == 1
    descs = {
        e.description
        for e in (await db_session.execute(select(ExpenseEntry))).scalars().all()
    }
    assert descs == {"Agua"}

    # La fila perdida ahora tiene rastro, y con la entidad sugerida correcta: es lo
    # que permite completarla desde /otros en vez de volver a cargar el archivo.
    capturadas = await _otros()
    assert len(capturadas) == 1
    assert capturadas[0].suggested_entity == "expense"
    assert "monto" in (capturadas[0].context_label or "").lower()

    # 2do import del MISMO archivo: ni un gasto nuevo ni una segunda copia en
    # "Otros". La huella quemada es lo que lo garantiza.
    second = await insert_confirmed_data(
        db_session, sample_tenant.tenant_id, _summary(""), {"gastos": True}, **kwargs
    )
    assert second["gastos"] == 0
    assert second["otros"] == 0
    assert len(await _otros()) == 1
    assert len((await db_session.execute(select(ExpenseEntry))).scalars().all()) == 1


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


async def test_multisheet_cuenta_montos_sin_escala_como_el_camino_plano(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """E7a-lite: el camino multihoja también publica ``montos_ambiguos``.

    El camino de tabla suelta ya lo contaba y lo publicaba; el multihoja
    **descartaba los motivos** de ``preparar_filas_de_hoja``, así que el mismo
    archivo con los mismos montos ilegibles dejaba el contador en ``None`` según
    cómo estuviera armado. Ese contador no es decorativo: discrimina el mensaje de
    ``EmptyImportError`` (escala ambigua vs. import vacío genérico) y es lo que
    aparece en el log cuando hay que explicarle al usuario por qué su archivo de
    números «no entró».
    """
    import uuid as _uuid

    from app.persistence.models.file import UploadedFile

    uploaded = UploadedFile(
        id=_uuid.uuid4(),
        tenant_id=sample_tenant.tenant_id,
        original_filename="gastos_escala.xlsx",
        s3_key="test/gastos_escala.xlsx",
        content_type="application/vnd.ms-excel",
        size_bytes=1024,
        purpose="ingestion",
    )
    db_session.add(uploaded)
    await db_session.flush()

    # La columna mezcla "12.500" (miles AR) con "12.50" (decimal US): no hay un
    # convenio que explique las tres celdas, así que ninguna se interpreta. No se
    # adivina — cada fila va a "Otros" con el motivo.
    summary: dict[str, Any] = {
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
                "row_count": 3,
            }
        ],
        "ventas_detectadas": [],
        "gastos_detectados": [
            {
                "fecha": "2024-01-15",
                "concepto": "Luz",
                "monto": "12.500",
                "__context__": "sheet:Gastos",
            },
            {
                "fecha": "2024-01-16",
                "concepto": "Agua",
                "monto": "8.750",
                "__context__": "sheet:Gastos",
            },
            {
                "fecha": "2024-01-17",
                "concepto": "Gas",
                "monto": "12.50",
                "__context__": "sheet:Gastos",
            },
        ],
        "stock_detectado": [],
    }

    counts = await insert_confirmed_data(
        db_session,
        sample_tenant.tenant_id,
        summary,
        {"gastos": True},
        context_confirmed={"sheet:Gastos": True},
        uploaded_file_id=uploaded.id,
    )

    assert counts["gastos"] == 0
    assert counts["montos_ambiguos"] == 3
    assert counts["otros"] == 3
