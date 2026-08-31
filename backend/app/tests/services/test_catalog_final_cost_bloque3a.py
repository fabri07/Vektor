"""Bloque 3A — "compra+envío" como costo final del catálogo (Asteria).

Diagnóstico real: la hoja "precios y stock" de Asteria trae "Precio de compra"
(costo base) y, para algunos productos, "compra+envío" (costo final ya
calculado). El motor de distribución de flete de compras (F-H6) NUNCA corrió
sobre catálogo — solo sobre hojas de gastos/compras (`entity == "expense"`,
ver `_planificar_costos_de_la_hoja`) — así que no hay riesgo de sumar el envío
una segunda vez: esto es puro mapeo de columna, no cálculo.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.ingestion_import_service import insert_confirmed_data
from app.config.settings import get_settings
from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant

pytestmark = pytest.mark.asyncio


def _summary(rows: list[dict[str, Any]], headers: list[str]) -> dict[str, Any]:
    return {
        "file_type": "spreadsheet",
        "inferred_type": "mixed",
        "multi_sheet": True,
        "has_stock": True,
        "mapping_contexts": [
            {
                "context_id": "sheet:Catalogo",
                "label": "Catalogo",
                "entity_type": "product",
                "headers": headers,
                "row_count": len(rows),
            },
        ],
        "stock_detectado": rows,
    }


def _enable(monkeypatch: pytest.MonkeyPatch, tenant_id: Any) -> None:
    monkeypatch.setattr(
        get_settings(),
        "CATALOG_FINAL_COST_ROLLOUT_TENANT_IDS",
        [str(tenant_id)],
    )


async def _one_product(session: AsyncSession, tenant_id: Any, name: str) -> Product:
    return (
        await session.execute(
            select(Product).where(Product.tenant_id == tenant_id, Product.name == name)
        )
    ).scalar_one()


async def test_compra_mas_envio_gana_sobre_precio_de_compra(
    db_session: AsyncSession, sample_tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    tid = sample_tenant.tenant_id
    _enable(monkeypatch, tid)

    row = {
        "nombre": "Silla de living",
        "precio_de_compra": "1000",
        "compra+envio": "1150",
        "%_envio": "15",
        "precio_venta": "2000",
        "__context__": "sheet:Catalogo",
    }
    summary = _summary(
        [row], ["nombre", "precio_de_compra", "compra+envio", "%_envio", "precio_venta"]
    )
    await insert_confirmed_data(
        db_session,
        tid,
        summary,
        {"productos": True},
        context_confirmed={"sheet:Catalogo": True},
    )

    product = await _one_product(db_session, tid, "Silla de living")
    assert product.unit_cost_ars == Decimal("1150")


async def test_no_se_suma_el_envio_dos_veces(
    db_session: AsyncSession, sample_tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """1150 = 1000 + 15% (aprox). El costo final NO debe ser 1000*1.15*1.15
    ni 1150 + 15% adicional — es un mapeo directo, no un cálculo."""
    tid = sample_tenant.tenant_id
    _enable(monkeypatch, tid)

    row = {
        "nombre": "Mesa ratona",
        "precio_de_compra": "1000",
        "compra+envio": "1150",
        "%_envio": "15",
        "precio_venta": "2000",
        "__context__": "sheet:Catalogo",
    }
    summary = _summary(
        [row], ["nombre", "precio_de_compra", "compra+envio", "%_envio", "precio_venta"]
    )
    await insert_confirmed_data(
        db_session,
        tid,
        summary,
        {"productos": True},
        context_confirmed={"sheet:Catalogo": True},
    )

    product = await _one_product(db_session, tid, "Mesa ratona")
    assert product.unit_cost_ars == Decimal("1150")
    assert product.unit_cost_ars != Decimal("1150") * Decimal("1.15")


async def test_se_conservan_costo_base_y_porcentaje(
    db_session: AsyncSession, sample_tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    tid = sample_tenant.tenant_id
    _enable(monkeypatch, tid)

    row = {
        "nombre": "Lámpara de pie",
        "precio_de_compra": "800",
        "compra+envio": "920",
        "%_envio": "15",
        "precio_venta": "1800",
        "__context__": "sheet:Catalogo",
    }
    summary = _summary(
        [row], ["nombre", "precio_de_compra", "compra+envio", "%_envio", "precio_venta"]
    )
    await insert_confirmed_data(
        db_session,
        tid,
        summary,
        {"productos": True},
        context_confirmed={"sheet:Catalogo": True},
    )

    product = await _one_product(db_session, tid, "Lámpara de pie")
    assert product.custom_fields.get("purchase_base_cost") == "800"
    assert product.custom_fields.get("shipping_percentage") == "15"
    assert product.unit_cost_ars == Decimal("920")


async def test_mapeo_manual_gana(
    db_session: AsyncSession, sample_tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Un mapeo explícito a unit_cost_ars gana SIEMPRE, con o sin flag."""
    tid = sample_tenant.tenant_id
    _enable(monkeypatch, tid)

    row = {
        "nombre": "Espejo de pared",
        "precio_de_compra": "500",
        "compra+envio": "600",
        "costo_real": "777",
        "precio_venta": "1500",
        "__context__": "sheet:Catalogo",
    }
    summary = _summary(
        [row], ["nombre", "precio_de_compra", "compra+envio", "costo_real", "precio_venta"]
    )
    await insert_confirmed_data(
        db_session,
        tid,
        summary,
        {"productos": True},
        context_mappings={"sheet:Catalogo": {"costo_real": "unit_cost_ars"}},
        context_confirmed={"sheet:Catalogo": True},
    )

    product = await _one_product(db_session, tid, "Espejo de pared")
    assert product.unit_cost_ars == Decimal("777")


async def test_sin_compra_mas_envio_hay_fallback_compatible(
    db_session: AsyncSession, sample_tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Solo "Precio de compra": se sigue usando como costo final, como siempre."""
    tid = sample_tenant.tenant_id
    _enable(monkeypatch, tid)

    row = {
        "nombre": "Cortina blackout",
        "precio_de_compra": "400",
        "precio_venta": "900",
        "__context__": "sheet:Catalogo",
    }
    summary = _summary([row], ["nombre", "precio_de_compra", "precio_venta"])
    await insert_confirmed_data(
        db_session,
        tid,
        summary,
        {"productos": True},
        context_confirmed={"sheet:Catalogo": True},
    )

    product = await _one_product(db_session, tid, "Cortina blackout")
    assert product.unit_cost_ars == Decimal("400")
    assert product.custom_fields.get("purchase_base_cost") == "400"


async def test_relectura_doble_produce_el_mismo_costo_y_custom_fields(
    db_session: AsyncSession, sample_tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    tid = sample_tenant.tenant_id
    _enable(monkeypatch, tid)

    row = {
        "nombre": "Alfombra redonda",
        "precio_de_compra": "600",
        "compra+envio": "690",
        "%_envio": "15",
        "precio_venta": "1400",
        "__context__": "sheet:Catalogo",
    }
    summary = _summary(
        [row], ["nombre", "precio_de_compra", "compra+envio", "%_envio", "precio_venta"]
    )
    for _ in range(2):
        await insert_confirmed_data(
            db_session,
            tid,
            summary,
            {"productos": True},
            context_confirmed={"sheet:Catalogo": True},
        )
        await db_session.commit()

    products = (
        await db_session.execute(
            select(Product).where(Product.tenant_id == tid, Product.name == "Alfombra redonda")
        )
    ).scalars().all()
    assert len(products) == 1
    assert products[0].unit_cost_ars == Decimal("690")
    assert products[0].custom_fields.get("purchase_base_cost") == "600"
    assert products[0].custom_fields.get("shipping_percentage") == "15"


async def test_flag_apagado_conserva_el_comportamiento_actual(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Sin habilitar el rollout, "compra+envío" NO se prioriza — el orden de
    columnas del archivo decide como hasta ahora (comportamiento histórico:
    la PRIMERA columna del archivo que matchea "compra" gana)."""
    tid = sample_tenant.tenant_id
    assert get_settings().CATALOG_FINAL_COST_ROLLOUT_TENANT_IDS == []

    row = {
        "nombre": "Portarretrato",
        "precio_de_compra": "300",
        "compra+envio": "345",
        "precio_venta": "700",
        "__context__": "sheet:Catalogo",
    }
    summary = _summary([row], ["nombre", "precio_de_compra", "compra+envio", "precio_venta"])
    await insert_confirmed_data(
        db_session,
        tid,
        summary,
        {"productos": True},
        context_confirmed={"sheet:Catalogo": True},
    )

    product = await _one_product(db_session, tid, "Portarretrato")
    # "precio_de_compra" aparece PRIMERO en la fila → gana por orden, como
    # siempre hizo _COSTO_UNITARIO_PRODUCT_COLS (set, no tupla de prioridad).
    assert product.unit_cost_ars == Decimal("300")
