"""Tests de la bandeja "Otros" (unclassified_records).

Cubre: captura desde el import (filas ambiguas no reasignadas), import por
reasignación explícita (el flujo existente de confirmación no se rompe), y
captura de hojas no clasificables en multi-hoja.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.ingestion_import_service import insert_confirmed_data
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import SaleEntry
from app.persistence.models.unclassified_record import UnclassifiedRecord


def _general_summary() -> dict[str, Any]:
    """Archivo ambiguo: tipo 'general', filas en otros_detectados (FASE F)."""
    return {
        "file_type": "spreadsheet",
        "inferred_type": "general",
        "row_count": 2,
        "otros_detectados": [
            {"fecha": "2026-05-01", "detalle": "Pago Juan", "monto": "1000"},
            {"fecha": "2026-05-02", "detalle": "Cobro Ana", "monto": "2000"},
        ],
        "ventas_detectadas": [],
        "gastos_detectados": [],
        "stock_detectado": [],
    }


@pytest.mark.asyncio
async def test_general_sin_confirmacion_va_a_otros(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    counts = await insert_confirmed_data(
        db_session,
        sample_tenant.tenant_id,
        _general_summary(),
        {"ventas": False, "gastos": False, "productos": False},
        source="ingestion",
    )
    assert counts["otros"] == 2
    assert counts["ventas"] == 0

    records = (await db_session.execute(select(UnclassifiedRecord))).scalars().all()
    assert len(records) == 2
    assert records[0].status == "PENDING"
    assert records[0].source == "ingestion"
    assert records[0].row_data["detalle"] in {"Pago Juan", "Cobro Ana"}


@pytest.mark.asyncio
async def test_general_confirmado_como_ventas_importa_normal(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """La confirmación explícita del usuario sigue importando archivos ambiguos."""
    counts = await insert_confirmed_data(
        db_session,
        sample_tenant.tenant_id,
        _general_summary(),
        {"ventas": True},
    )
    assert counts["ventas"] == 2
    assert counts["otros"] == 0
    assert (await db_session.execute(select(UnclassifiedRecord))).first() is None
    sales = (await db_session.execute(select(SaleEntry))).scalars().all()
    assert len(sales) == 2


@pytest.mark.asyncio
async def test_multisheet_hoja_no_clasificada_va_a_otros(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    summary: dict[str, Any] = {
        "file_type": "spreadsheet",
        "inferred_type": "mixed",
        "multi_sheet": True,
        "mapping_contexts": [
            {
                "context_id": "sheet:Ventas",
                "label": "Ventas",
                "entity_type": "sale",
                "headers": ["fecha", "monto"],
            },
            {
                "context_id": "sheet:Rara",
                "label": "Rara",
                "entity_type": None,
                "unclassified": True,
                "headers": ["x", "y"],
            },
        ],
        "ventas_detectadas": [
            {"fecha": "2026-05-01", "monto": "500", "__context__": "sheet:Ventas"}
        ],
        "gastos_detectados": [],
        "stock_detectado": [],
        "otros_detectados": [
            {"x": "a", "y": "b", "__context__": "sheet:Rara"},
        ],
    }
    counts = await insert_confirmed_data(
        db_session,
        sample_tenant.tenant_id,
        summary,
        {"ventas": True},
        source="chat",
    )
    assert counts["ventas"] == 1
    assert counts["otros"] == 1
    record = (await db_session.execute(select(UnclassifiedRecord))).scalar_one()
    assert record.context_label == "Rara"
    assert record.source == "chat"


@pytest.mark.asyncio
async def test_multisheet_hoja_reasignada_se_importa(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """context_entity reasigna una hoja no clasificada a un tipo importable."""
    summary: dict[str, Any] = {
        "file_type": "spreadsheet",
        "inferred_type": "mixed",
        "multi_sheet": True,
        "mapping_contexts": [
            {
                "context_id": "sheet:Rara",
                "label": "Rara",
                "entity_type": None,
                "unclassified": True,
                "headers": ["fecha", "monto"],
            },
        ],
        "ventas_detectadas": [],
        "gastos_detectados": [],
        "stock_detectado": [],
        "otros_detectados": [
            {"fecha": "2026-05-01", "monto": "750", "__context__": "sheet:Rara"},
        ],
    }
    counts = await insert_confirmed_data(
        db_session,
        sample_tenant.tenant_id,
        summary,
        {"ventas": True},
        context_entity={"sheet:Rara": "sale"},
        context_confirmed={"sheet:Rara": True},
    )
    assert counts["ventas"] == 1
    assert counts["otros"] == 0
    sale = (await db_session.execute(select(SaleEntry))).scalar_one()
    assert str(sale.amount) == "750.00"
