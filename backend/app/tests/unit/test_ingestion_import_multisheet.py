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

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.ingestion_import_service import insert_confirmed_data
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import ExpenseEntry, SaleEntry


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


@pytest.mark.asyncio
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


@pytest.mark.asyncio
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


@pytest.mark.asyncio
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


@pytest.mark.asyncio
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
    assert sale.custom_fields == {"vendedor": "Juan"}
    # El marcador interno nunca se filtra a custom_fields.
    assert "__context__" not in sale.custom_fields


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


@pytest.mark.asyncio
async def test_text_contexts_imported_by_group(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Documento de texto: cada grupo incluido se importa con su entity_type."""
    counts = await insert_confirmed_data(
        db_session,
        sample_tenant.tenant_id,
        _text_summary(),
        {},
        context_confirmed={"text:sale": True, "text:expense": True},
    )
    assert counts["ventas"] == 1
    assert counts["gastos"] == 1
    sale = (await db_session.execute(select(SaleEntry))).scalar_one()
    assert sale.amount == Decimal("5000")


@pytest.mark.asyncio
async def test_text_context_entity_override(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """context_entity reasigna el grupo 'ventas' detectado a gasto."""
    counts = await insert_confirmed_data(
        db_session,
        sample_tenant.tenant_id,
        _text_summary(),
        {},
        context_confirmed={"text:sale": True, "text:expense": False},
        context_entity={"text:sale": "expense"},
    )
    # El grupo de ventas, reasignado a gasto, entra como ExpenseEntry.
    assert counts["ventas"] == 0
    assert counts["gastos"] == 1
    expense = (await db_session.execute(select(ExpenseEntry))).scalar_one()
    assert expense.amount == Decimal("5000")
