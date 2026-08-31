"""Bloque 3B — sugerencia de categoría de producto, integrada en el import real.

La inferencia pura vive en `test_product_category_inference.py` (domain). Acá
se prueba el WIRING: alta confianza se aplica a `product.category`, media
queda como sugerencia (no se aplica), y una categoría ya confirmada —a mano o
por una relectura anterior— nunca se pisa (mismo guard que ya protegía
`existing.category` de la fase E, `if cat and not existing.category`).
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.ingestion_import_service import insert_confirmed_data
from app.persistence.models.business import BusinessProfile
from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant

pytestmark = pytest.mark.asyncio


def _summary(rows: list[dict[str, Any]], headers: list[str]) -> dict[str, Any]:
    tagged_rows = [{**r, "__context__": "sheet:Catalogo"} for r in rows]
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
        "stock_detectado": tagged_rows,
    }


async def _one_product(session: AsyncSession, tenant_id: Any, name: str) -> Product:
    return (
        await session.execute(
            select(Product).where(Product.tenant_id == tenant_id, Product.name == name)
        )
    ).scalar_one()


async def test_categorias_representativas_de_decoracion_hogar_se_aplican(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """sample_tenant ya tiene BusinessProfile vertical=kiosco_almacen — se
    modifica a decoracion_hogar para esta prueba."""
    from sqlalchemy import update

    await db_session.execute(
        update(BusinessProfile)
        .where(BusinessProfile.tenant_id == sample_tenant.tenant_id)
        .values(vertical_code="decoracion_hogar")
    )
    await db_session.commit()
    tid = sample_tenant.tenant_id

    row = {"nombre": "Silla de living tapizada", "precio_venta": "5000"}
    await insert_confirmed_data(
        db_session,
        tid,
        _summary([row], ["nombre", "precio_venta"]),
        {"productos": True},
        context_confirmed={"sheet:Catalogo": True},
    )

    product = await _one_product(db_session, tid, "Silla de living tapizada")
    assert product.category == "MUEBLES"
    assert product.custom_fields.get("category_suggestion_confidence") == "high"


async def test_especificacion_complementa_nombre_ambiguo_queda_como_sugerencia(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    from sqlalchemy import update

    await db_session.execute(
        update(BusinessProfile)
        .where(BusinessProfile.tenant_id == sample_tenant.tenant_id)
        .values(vertical_code="decoracion_hogar")
    )
    await db_session.commit()
    tid = sample_tenant.tenant_id

    row = {
        "nombre": "Combo x3 unidades",
        "especificaciones": "Vela aromática de soja, esencia de lavanda",
        "precio_venta": "1200",
    }
    await insert_confirmed_data(
        db_session,
        tid,
        _summary([row], ["nombre", "especificaciones", "precio_venta"]),
        {"productos": True},
        context_confirmed={"sheet:Catalogo": True},
    )

    product = await _one_product(db_session, tid, "Combo x3 unidades")
    # Media confianza: NO se aplica directo, queda como sugerencia con evidencia.
    assert product.category is None
    assert product.custom_fields.get("category_suggestion_code") == "AROMAS"
    assert product.custom_fields.get("category_suggestion_confidence") == "medium"


async def test_baja_confianza_no_categoriza(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    from sqlalchemy import update

    await db_session.execute(
        update(BusinessProfile)
        .where(BusinessProfile.tenant_id == sample_tenant.tenant_id)
        .values(vertical_code="decoracion_hogar")
    )
    await db_session.commit()
    tid = sample_tenant.tenant_id

    row = {"nombre": "Producto genérico X123", "precio_venta": "500"}
    await insert_confirmed_data(
        db_session,
        tid,
        _summary([row], ["nombre", "precio_venta"]),
        {"productos": True},
        context_confirmed={"sheet:Catalogo": True},
    )

    product = await _one_product(db_session, tid, "Producto genérico X123")
    assert product.category is None
    assert "category_suggestion_code" not in product.custom_fields


async def test_categoria_manual_preservada(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Una categoría ya confirmada a mano NO se pisa en una relectura futura,
    aunque el nombre matchee otra categoría distinta con alta confianza."""
    from sqlalchemy import update

    await db_session.execute(
        update(BusinessProfile)
        .where(BusinessProfile.tenant_id == sample_tenant.tenant_id)
        .values(vertical_code="decoracion_hogar")
    )
    await db_session.commit()
    tid = sample_tenant.tenant_id

    row = {"nombre": "Silla de living tapizada", "precio_venta": "5000"}
    await insert_confirmed_data(
        db_session,
        tid,
        _summary([row], ["nombre", "precio_venta"]),
        {"productos": True},
        context_confirmed={"sheet:Catalogo": True},
    )
    product = await _one_product(db_session, tid, "Silla de living tapizada")
    assert product.category == "MUEBLES"

    # El usuario corrige a mano.
    product.category = "DECO"
    product.has_user_edits = True
    await db_session.commit()

    # Relectura del mismo archivo: la inferencia volvería a sugerir MUEBLES,
    # pero el producto YA tiene categoría → no se pisa.
    await insert_confirmed_data(
        db_session,
        tid,
        _summary([row], ["nombre", "precio_venta"]),
        {"productos": True},
        context_confirmed={"sheet:Catalogo": True},
        source="reread",
    )
    await db_session.refresh(product)
    assert product.category == "DECO"


async def test_segunda_relectura_no_cambia_una_decision_confirmada(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    from sqlalchemy import update

    await db_session.execute(
        update(BusinessProfile)
        .where(BusinessProfile.tenant_id == sample_tenant.tenant_id)
        .values(vertical_code="decoracion_hogar")
    )
    await db_session.commit()
    tid = sample_tenant.tenant_id

    row = {"nombre": "Lámpara de pie con pantalla", "precio_venta": "3000"}
    summary = _summary([row], ["nombre", "precio_venta"])
    for _ in range(2):
        await insert_confirmed_data(
            db_session,
            tid,
            summary,
            {"productos": True},
            context_confirmed={"sheet:Catalogo": True},
            source="reread",
        )
        await db_session.commit()

    products = (
        await db_session.execute(
            select(Product).where(
                Product.tenant_id == tid, Product.name == "Lámpara de pie con pantalla"
            )
        )
    ).scalars().all()
    assert len(products) == 1
    assert products[0].category == "ILUMINACION"


async def test_aislamiento_entre_verticales(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """sample_tenant queda en su vertical default (kiosco_almacen) — un nombre
    que matchearía MUEBLES en decoración del hogar no sugiere nada acá, porque
    ese catálogo de inferencia no existe para kiosco."""
    tid = sample_tenant.tenant_id

    row = {"nombre": "Silla de living tapizada", "precio_venta": "5000"}
    await insert_confirmed_data(
        db_session,
        tid,
        _summary([row], ["nombre", "precio_venta"]),
        {"productos": True},
        context_confirmed={"sheet:Catalogo": True},
    )

    product = await _one_product(db_session, tid, "Silla de living tapizada")
    assert product.category is None
