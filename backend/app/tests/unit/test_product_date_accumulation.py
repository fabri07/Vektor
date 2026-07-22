"""F6-B2: fechas de producto (acquired_at/expiry_date) — helpers de acumulación
y su cableado en el importador de catálogos."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.ingestion_import_service import (
    _accumulate_acquired_at,
    _accumulate_expiry_date,
    insert_confirmed_data,
)
from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant

# ── Helpers puros ─────────────────────────────────────────────────────────────


def test_accumulate_acquired_at_toma_la_mas_antigua() -> None:
    a = datetime(2026, 3, 1)
    b = datetime(2026, 1, 15)
    assert _accumulate_acquired_at(a, b) == b  # la más antigua
    assert _accumulate_acquired_at(None, b) == b
    assert _accumulate_acquired_at(a, None) == a
    assert _accumulate_acquired_at(None, None) is None


def test_accumulate_expiry_prefiere_el_futuro_mas_proximo() -> None:
    today = date(2026, 6, 1)
    # Dos futuros → el más próximo.
    assert _accumulate_expiry_date(date(2026, 12, 1), date(2026, 7, 1), today) == date(
        2026, 7, 1
    )
    # Uno futuro, uno vencido → el futuro.
    assert _accumulate_expiry_date(date(2026, 5, 1), date(2026, 9, 1), today) == date(
        2026, 9, 1
    )
    # Hoy cuenta como no-vencido (>=).
    assert _accumulate_expiry_date(today, date(2026, 1, 1), today) == today


def test_accumulate_expiry_todos_vencidos_toma_el_mas_reciente() -> None:
    today = date(2026, 6, 1)
    assert _accumulate_expiry_date(date(2026, 1, 1), date(2026, 4, 1), today) == date(
        2026, 4, 1
    )
    assert _accumulate_expiry_date(None, None, today) is None
    assert _accumulate_expiry_date(date(2026, 3, 1), None, today) == date(2026, 3, 1)


# ── Integración: importador de catálogo ───────────────────────────────────────


def _r(alta: str, venc: str = "10/07/2026", stock: str = "5") -> dict[str, Any]:
    return {"producto": "Leche", "vencimiento": venc, "fecha_alta": alta, "stock": stock}


_MAPPING = {
    "producto": "name",
    "vencimiento": "expiry_date",
    "fecha_alta": "acquired_at",
    "stock": "stock_units",
}


async def _import_multi(
    db_session: AsyncSession, tid: uuid.UUID, rows: list[dict[str, Any]]
) -> None:
    """Path multi-hoja (_insert_multisheet_data): inferred_type mixed/multi_sheet."""
    summary = {
        "file_type": "spreadsheet",
        "inferred_type": "stock",
        "multi_sheet": True,
        "has_producto": True,
        "mapping_contexts": [
            {
                "context_id": "sheet:Catalogo",
                "entity_type": "product",
                "source_kind": "sheet",
                "headers": ["producto", "vencimiento", "fecha_alta", "stock"],
                "row_count": len(rows),
            }
        ],
        "stock_detectado": [{**r, "__context__": "sheet:Catalogo"} for r in rows],
    }
    await insert_confirmed_data(
        db_session,
        tid,
        summary,
        {"productos": True},
        context_confirmed={"sheet:Catalogo": True},
        context_mappings={"sheet:Catalogo": _MAPPING},
    )


async def _import_single(
    db_session: AsyncSession, tid: uuid.UUID, rows: list[dict[str, Any]]
) -> None:
    """Path single-context (catálogo CSV/XLSX simple): inferred_type=stock SIN
    multi_sheet → el camino común, el que el review encontró sin cablear."""
    summary = {
        "file_type": "spreadsheet",
        "inferred_type": "stock",
        "has_producto": True,
        "stock_detectado": list(rows),
    }
    await insert_confirmed_data(
        db_session, tid, summary, {"productos": True}, column_mappings=_MAPPING
    )


# Ambos caminos deben cumplir el mismo contrato B1/B2 (paridad CSV/XLSX).
_IMPORTERS = [
    pytest.param(_import_single, id="single-context"),
    pytest.param(_import_multi, id="multi-sheet"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("do_import", _IMPORTERS)
async def test_importar_catalogo_puebla_fechas_de_producto(
    db_session: AsyncSession, sample_tenant: Tenant, do_import: Any
) -> None:
    tid = sample_tenant.tenant_id
    await do_import(db_session, tid, [_r("01/03/2026")])
    await db_session.commit()
    prod = (await db_session.execute(select(Product))).scalar_one()
    assert prod.expiry_date == date(2026, 7, 10)
    assert prod.acquired_at is not None
    assert prod.acquired_at.date() == date(2026, 3, 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("do_import", _IMPORTERS)
async def test_reimportar_acumula_acquired_mas_antigua(
    db_session: AsyncSession, sample_tenant: Tenant, do_import: Any
) -> None:
    tid = sample_tenant.tenant_id
    await do_import(db_session, tid, [_r("01/03/2026")])
    await db_session.commit()
    await do_import(db_session, tid, [_r("01/01/2026")])
    await db_session.commit()
    prod = (await db_session.execute(select(Product))).scalar_one()
    assert prod.acquired_at is not None
    assert prod.acquired_at.date() == date(2026, 1, 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("do_import", _IMPORTERS)
async def test_has_user_edits_protege_las_fechas(
    db_session: AsyncSession, sample_tenant: Tenant, do_import: Any
) -> None:
    tid = sample_tenant.tenant_id
    await do_import(db_session, tid, [_r("01/03/2026")])
    prod = (await db_session.execute(select(Product))).scalar_one()
    prod.has_user_edits = True
    # Valores que la regla de acumulación SOBRESCRIBIRÍA si el guard no existiera:
    # acquired_at manual MÁS NUEVA que la entrante (min la bajaría); expiry manual
    # futuro MÁS LEJANO que el entrante (futuro-más-próximo la adelantaría). Así el
    # test discrimina de verdad el guard (mutación: quitarlo → falla).
    prod.acquired_at = datetime(2026, 6, 1)
    prod.expiry_date = date(2027, 12, 1)
    await db_session.commit()

    # Reimporta con alta más antigua (01/01/2026) y venc futuro más próximo (01/09/2026).
    await do_import(db_session, tid, [_r("01/01/2026", venc="01/09/2026", stock="8")])
    await db_session.commit()
    prod2 = (await db_session.execute(select(Product))).scalar_one()
    assert prod2.acquired_at == datetime(2026, 6, 1)
    assert prod2.expiry_date == date(2027, 12, 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("do_import", _IMPORTERS)
async def test_fecha_mapeada_a_mano_ilegible_va_a_otros_sin_crear_producto(
    db_session: AsyncSession, sample_tenant: Tenant, do_import: Any
) -> None:
    """F6-B2 (política de inválida): un vencimiento MAPEADO a mano con valor no
    vacío ilegible → la fila va a /otros y NO se crea el producto (ni stock)."""
    from app.persistence.models.unclassified_record import UnclassifiedRecord

    tid = sample_tenant.tenant_id
    await do_import(db_session, tid, [_r("01/03/2026", venc="no es fecha")])
    await db_session.commit()

    assert (await db_session.execute(select(Product))).scalars().all() == []
    rec = (await db_session.execute(select(UnclassifiedRecord))).scalar_one()
    assert rec.suggested_entity == "product"
