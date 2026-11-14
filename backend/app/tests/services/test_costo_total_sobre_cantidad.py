"""Costo TOTAL de la línea ÷ cantidad = costo unitario (PR B, 2026-11-14).

Un archivo de compras trae «Costo total» (cantidad × unitario). Cargarlo como costo
unitario inflaba el inventario por la cantidad (#246 lo frenó como duda). Ahora, con
el campo `line_total_cost_ars` mapeado, el import divide por la cantidad de la
MISMA fila. Sin divisor positivo no se inventa nada.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.column_mapping_service import read_header
from app.application.services.ingestion_import_service import (
    _costo_unitario_desde_total,
    insert_confirmed_data,
)
from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant

pytestmark = pytest.mark.asyncio

_CTX = "sheet:Compras"


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "file_type": "spreadsheet",
        "inferred_type": "mixed",
        "multi_sheet": True,
        "has_stock": True,
        "mapping_contexts": [
            {
                "context_id": _CTX,
                "label": "Compras",
                "entity_type": "product",
                "headers": ["producto", "cantidad", "costo_total"],
                "row_count": len(rows),
            },
        ],
        "stock_detectado": [{**r, "__context__": _CTX} for r in rows],
    }


async def _importar(
    session: AsyncSession, tenant: Tenant, rows: list[dict[str, Any]], mapeo: dict[str, str]
) -> dict[str, Product]:
    await insert_confirmed_data(
        session,
        tenant.tenant_id,
        _summary(rows),
        {"productos": True},
        context_mappings={_CTX: mapeo},
        context_confirmed={_CTX: True},
    )
    prods = (await session.execute(select(Product))).scalars().all()
    return {str(p.name): p for p in prods}


_MAPEO_TOTAL = {
    "producto": "name",
    "cantidad": "stock_units",
    "costo_total": "line_total_cost_ars",
}


async def test_el_costo_total_se_divide_por_la_cantidad(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    prods = await _importar(
        db_session,
        sample_tenant,
        [{"producto": "Campera negra", "cantidad": "10", "costo_total": "320000"}],
        _MAPEO_TOTAL,
    )
    p = prods["Campera negra"]
    assert p.unit_cost_ars == Decimal("32000.00")  # no 320000
    assert p.stock_units == 10


async def test_sin_cantidad_no_se_inventa_el_costo(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    prods = await _importar(
        db_session,
        sample_tenant,
        [
            {"producto": "Sin cantidad", "cantidad": "", "costo_total": "5000"},
            {"producto": "Cantidad cero", "cantidad": "0", "costo_total": "5000"},
        ],
        _MAPEO_TOTAL,
    )
    assert prods["Sin cantidad"].unit_cost_ars is None
    assert prods["Cantidad cero"].unit_cost_ars is None
    assert prods["Sin cantidad"].requires_completion is True


async def test_un_costo_unitario_mapeado_gana_sobre_el_total(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    prods = await _importar(
        db_session,
        sample_tenant,
        [
            {
                "producto": "Jean",
                "cantidad": "4",
                "costo_total": "100000",
                "costo_unitario": "20000",
            }
        ],
        {**_MAPEO_TOTAL, "costo_unitario": "unit_cost_ars"},
    )
    assert prods["Jean"].unit_cost_ars == Decimal("20000.00")


def test_redondeo_a_dos_decimales_hacia_arriba() -> None:
    assert _costo_unitario_desde_total("100", 3) == Decimal("33.33")
    assert _costo_unitario_desde_total("200", 3) == Decimal("66.67")


@pytest.mark.parametrize(("total", "cantidad"), [("1000", 0), ("1000", -2), ("-5", 2), (None, 3)])
def test_sin_divisor_o_total_valido_no_hay_costo(total: Any, cantidad: int) -> None:
    assert _costo_unitario_desde_total(total, cantidad) is None


def test_el_encabezado_costo_total_apunta_al_campo_de_total() -> None:
    assert read_header("costo_total", "product").target == "line_total_cost_ars"
    assert read_header("Costo", "product").target == "unit_cost_ars"
