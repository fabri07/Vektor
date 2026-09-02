"""Tests de la bandeja "Otros" (unclassified_records).

Cubre: captura desde el import (filas ambiguas no reasignadas), import por
reasignación explícita (el flujo existente de confirmación no se rompe), y
captura de hojas no clasificables en multi-hoja.
"""

from __future__ import annotations

import uuid
from typing import Any

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


async def test_multisheet_hoja_no_clasificada_reimportada_no_duplica_otros(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Idempotencia real (bug destapado por el dry-run del Bloque 7 contra el
    Excel real de Asteria): releer el MISMO archivo (mismo ``uploaded_file_id``)
    con una hoja no clasificada y no reasignada NO debe duplicar sus filas en
    "Otros" — antes, ``_capture_unclassified`` no chequeaba ninguna huella y
    cada aplicación volvía a insertar todas las filas de la hoja."""
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
                "headers": ["x", "y"],
            },
        ],
        "ventas_detectadas": [],
        "gastos_detectados": [],
        "stock_detectado": [],
        "otros_detectados": [
            {"x": "a", "y": "b", "__context__": "sheet:Rara"},
            {"x": "c", "y": "d", "__context__": "sheet:Rara"},
        ],
    }
    uploaded_file_id = uuid.uuid4()

    counts_1 = await insert_confirmed_data(
        db_session,
        sample_tenant.tenant_id,
        summary,
        {"ventas": False},
        source="reread",
        uploaded_file_id=uploaded_file_id,
    )
    await db_session.commit()
    assert counts_1["otros"] == 2

    counts_2 = await insert_confirmed_data(
        db_session,
        sample_tenant.tenant_id,
        summary,
        {"ventas": False},
        source="reread",
        uploaded_file_id=uploaded_file_id,
    )
    await db_session.commit()
    assert counts_2["otros"] == 0

    records = (await db_session.execute(select(UnclassifiedRecord))).scalars().all()
    assert len(records) == 2


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


async def test_hoja_desmarcada_no_deja_absolutamente_ningun_rastro(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """INVARIANTE (pedido explícito del usuario): una hoja que el usuario NO
    selecciona no crea NADA — ni venta, ni gasto, ni producto, ni movimiento, ni
    un pendiente en "Otros".

    Antes, la hoja desmarcada igual dejaba sus filas en la bandeja: la captura a
    "Otros" del camino multi-hoja corría ANTES del chequeo de inclusión, así que
    "no importar" significaba "no crear entidades" pero no "no dejar rastro". Es
    el origen de 2.273 de los 2.288 pendientes de ASTERIA.
    """
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
                "context_id": "sheet:Ganancias",
                "label": "Ganancias",
                "entity_type": None,
                "unclassified": True,
                "headers": ["periodo", "total"],
            },
        ],
        "ventas_detectadas": [
            {"fecha": "2026-05-01", "monto": "500", "__context__": "sheet:Ventas"}
        ],
        "gastos_detectados": [],
        "stock_detectado": [],
        "otros_detectados": [
            {"periodo": "Enero", "total": "99999", "__context__": "sheet:Ganancias"},
            {"periodo": "Febrero", "total": "88888", "__context__": "sheet:Ganancias"},
        ],
    }
    counts = await insert_confirmed_data(
        db_session,
        sample_tenant.tenant_id,
        summary,
        {"ventas": True},
        source="ingestion",
        uploaded_file_id=None,
        # El usuario desmarcó "Ganancias" explícitamente.
        context_confirmed={"sheet:Ventas": True, "sheet:Ganancias": False},
    )
    assert counts["ventas"] == 1, "la hoja incluida sí se importa"
    assert counts["otros"] == 0, "la hoja desmarcada no deja pendientes"
    assert (await db_session.execute(select(UnclassifiedRecord))).scalars().all() == []


async def test_hoja_derivada_no_captura_aunque_no_haya_decision_explicita(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Una hoja de resumen/derivada no se captura ni cuando el usuario no dijo
    nada: es Véktor el que la detecta, y capturarla llenaría la bandeja con
    totales que no son operaciones."""
    summary: dict[str, Any] = {
        "file_type": "spreadsheet",
        "inferred_type": "mixed",
        "multi_sheet": True,
        "mapping_contexts": [
            {
                "context_id": "sheet:Ganancias",
                "label": "Ganancias",
                "entity_type": None,
                "unclassified": True,
                "is_summary_or_derived": True,
                "headers": ["periodo", "total"],
            },
        ],
        "ventas_detectadas": [],
        "gastos_detectados": [],
        "stock_detectado": [],
        "otros_detectados": [
            {"periodo": "Total", "total": "99999", "__context__": "sheet:Ganancias"},
        ],
    }
    counts = await insert_confirmed_data(
        db_session,
        sample_tenant.tenant_id,
        summary,
        {"ventas": True},
        source="ingestion",
    )
    assert counts["otros"] == 0
    assert (await db_session.execute(select(UnclassifiedRecord))).scalars().all() == []


async def test_hoja_ambigua_sin_decision_sigue_capturando(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """La red de FASE F se conserva. Una hoja que el parser no supo clasificar y
    sobre la que el usuario NO se pronunció sigue yendo a "Otros": ausencia de
    decisión no es un "no", y descartarla sería perder datos en silencio — el
    riesgo que el usuario eligió NO correr al acotar el descarte automático a las
    hojas derivadas."""
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
                "headers": ["x", "y"],
            },
        ],
        "ventas_detectadas": [],
        "gastos_detectados": [],
        "stock_detectado": [],
        "otros_detectados": [{"x": "a", "y": "b", "__context__": "sheet:Rara"}],
    }
    counts = await insert_confirmed_data(
        db_session, sample_tenant.tenant_id, summary, {"ventas": True}, source="ingestion"
    )
    assert counts["otros"] == 1


async def test_filas_en_blanco_no_materializan_pendientes(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Una fila de relleno de Excel llega con todas las columnas presentes y en
    ``None``: tiene claves, así que el guard viejo (``if not row_data``) la dejaba
    pasar y creaba un pendiente vacío que nadie puede clasificar. Son 314 de los
    2.288 de ASTERIA. Solo la fila con contenido queda."""
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
                "headers": ["x", "y"],
            },
        ],
        "ventas_detectadas": [],
        "gastos_detectados": [],
        "stock_detectado": [],
        "otros_detectados": [
            {"x": None, "y": None, "__context__": "sheet:Rara"},
            {"x": "", "y": "   ", "__context__": "sheet:Rara"},
            {"x": "nan", "y": "None", "__context__": "sheet:Rara"},
            {"x": "dato real", "y": None, "__context__": "sheet:Rara"},
        ],
    }
    counts = await insert_confirmed_data(
        db_session, sample_tenant.tenant_id, summary, {"ventas": True}, source="ingestion"
    )
    assert counts["otros"] == 1
    record = (await db_session.execute(select(UnclassifiedRecord))).scalar_one()
    assert record.row_data["x"] == "dato real"
