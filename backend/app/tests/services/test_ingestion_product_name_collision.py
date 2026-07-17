"""F1 (hotfix puente): el confirm de import NUNCA debe explotar (500) cuando el
tenant tiene ≥2 productos ACTIVOS cuyo nombre colisiona en
``lower(trim(name))``. Antes, tanto el camino single-sheet in-place
(``_insert_confirmed_data_impl``) como el multisheet (``_add_product`` dentro de
``_insert_multisheet_data``) usaban ``scalar_one_or_none()`` sobre un lookup por
nombre que podía devolver ≥2 filas → ``sqlalchemy.exc.MultipleResultsFound``.

Fase 1 NO cambia la semántica de identidad de producto (eso es Fase 2): solo
hace que el lookup ambiguo (count>=2) se detecte sin adivinar, no toque ningún
producto existente, no cree nada, y quede trazado en
``counts["productos_ambiguos"]`` + un warning de log.

Cubre también el caching intra-corrida (``products_by_norm_name``) que evita
crear 2 productos cuando el MISMO archivo trae 2 filas con el mismo nombre
en un solo bloque (<500 filas), reproduciendo el escenario real de prod
(``autoflush=False``) con ``isolated_db_engine``.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

import app.application.services.ingestion_import_service as importer
from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant


def _stock_summary(rows: list[dict[str, object]]) -> dict[str, object]:
    """Summary single-sheet (camino B: _insert_confirmed_data_impl in-place)."""
    return {
        "file_type": "spreadsheet",
        "inferred_type": "stock",
        "has_producto": True,
        "stock_detectado": rows,
    }


def _multisheet_product_summary(rows: list[dict[str, object]]) -> dict[str, object]:
    """Summary multi-hoja (camino A: _insert_multisheet_data → _add_product)."""
    return {
        "file_type": "spreadsheet",
        "inferred_type": "mixed",
        "multi_sheet": True,
        "mapping_contexts": [
            {
                "context_id": "sheet:Productos",
                "entity_type": "product",
                "source_kind": "sheet",
                "headers": ["producto", "precio", "costo", "stock"],
                "fields": None,
                "preview_rows": [],
                "row_count": len(rows),
            }
        ],
        "ventas_detectadas": [],
        "gastos_detectados": [],
        "stock_detectado": [
            {**row, "__context__": "sheet:Productos"} for row in rows
        ],
    }


async def _create_two_active_products_same_name(
    db_session: AsyncSession, tenant_id: uuid.UUID, name: str = "Coca Cola"
) -> list[Product]:
    """Dos productos activos preexistentes con el mismo nombre (colisión real)."""
    products = [
        Product(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            name=name,
            sale_price_ars=Decimal("1000"),
            unit_cost_ars=Decimal("600"),
            stock_units=5,
            is_active=True,
        ),
        Product(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            name=name,
            sale_price_ars=Decimal("1200"),
            unit_cost_ars=Decimal("700"),
            stock_units=8,
            is_active=True,
        ),
    ]
    for p in products:
        db_session.add(p)
    await db_session.commit()
    return products


# ── 1. Reproduce el bug (regresión) ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_single_sheet_duplicate_product_name_no_500(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Camino B (single-sheet in-place): 2 productos activos preexistentes con el
    mismo nombre + import que referencia ese nombre → NO explota, la fila queda
    sin importar, ningún producto existente se toca, y queda contabilizada como
    ambigua."""
    existing_products = await _create_two_active_products_same_name(
        db_session, sample_tenant.tenant_id
    )
    summary = _stock_summary(
        [{"producto": "Coca Cola", "precio": "1500", "costo": "900", "stock": "50"}]
    )

    counts = await importer.insert_confirmed_data(
        db_session, sample_tenant.tenant_id, summary, {"productos": True}
    )

    assert counts["productos_ambiguos"] == 1
    assert counts["productos"] == 0

    # Ningún producto existente fue tocado (ni precio ni stock).
    refreshed = (
        await db_session.execute(
            select(Product).where(Product.tenant_id == sample_tenant.tenant_id)
        )
    ).scalars().all()
    assert len(refreshed) == 2
    by_id = {p.id: p for p in refreshed}
    for original in existing_products:
        current = by_id[original.id]
        assert current.sale_price_ars == original.sale_price_ars
        assert current.stock_units == original.stock_units


@pytest.mark.asyncio
async def test_multisheet_duplicate_product_name_no_500(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Camino A (multisheet _add_product): mismo escenario que arriba pero por el
    path multi-hoja."""
    existing_products = await _create_two_active_products_same_name(
        db_session, sample_tenant.tenant_id
    )
    summary = _multisheet_product_summary(
        [{"producto": "Coca Cola", "precio": "1500", "costo": "900", "stock": "50"}]
    )

    counts = await importer.insert_confirmed_data(
        db_session,
        sample_tenant.tenant_id,
        summary,
        {"productos": True},
        context_confirmed={"sheet:Productos": True},
    )

    assert counts["productos_ambiguos"] == 1
    assert counts["productos"] == 0

    refreshed = (
        await db_session.execute(
            select(Product).where(Product.tenant_id == sample_tenant.tenant_id)
        )
    ).scalars().all()
    assert len(refreshed) == 2
    by_id = {p.id: p for p in refreshed}
    for original in existing_products:
        current = by_id[original.id]
        assert current.sale_price_ars == original.sale_price_ars
        assert current.stock_units == original.stock_units


# ── 2. Caché intra-corrida (evita duplicar con autoflush=False, patrón prod) ──


@pytest.mark.asyncio
async def test_single_sheet_two_rows_same_name_same_run_creates_one_product(
    isolated_db_engine: AsyncEngine,
) -> None:
    """Camino B: 2 filas con el MISMO nombre, sin producto preexistente, en un
    solo bloque (<500) → crea 1 solo producto (no 2). Reproduce prod
    (``autoflush=False``): sin la caché, el segundo SELECT no ve el producto
    recién agregado (pendiente sin flush) y duplicaría."""
    factory = async_sessionmaker(
        isolated_db_engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
    )
    async with factory() as session:
        tenant = Tenant(
            tenant_id=uuid.uuid4(),
            legal_name="K",
            display_name="K",
            currency="ARS",
            pricing_reference_mode="MEP",
            status="ACTIVE",
        )
        session.add(tenant)
        await session.commit()

        summary = _stock_summary(
            [
                {"producto": "Sprite 500ml", "precio": "1000", "costo": "600", "stock": "5"},
                {"producto": "Sprite 500ml", "precio": "1000", "costo": "600", "stock": "3"},
            ]
        )
        counts = await importer.insert_confirmed_data(
            session, tenant.tenant_id, summary, {"productos": True}
        )
        await session.commit()

        assert counts["productos"] == 2  # ambas filas se procesaron (create + update)
        assert counts["productos_ambiguos"] == 0

        products = (
            await session.execute(
                select(Product).where(Product.tenant_id == tenant.tenant_id)
            )
        ).scalars().all()
        assert len(products) == 1
        assert products[0].stock_units == 3  # 2da fila actualizó el mismo producto


@pytest.mark.asyncio
async def test_multisheet_two_rows_same_name_same_run_creates_one_product(
    isolated_db_engine: AsyncEngine,
) -> None:
    """Camino A: mismo escenario que arriba pero por _add_product (multisheet)."""
    factory = async_sessionmaker(
        isolated_db_engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
    )
    async with factory() as session:
        tenant = Tenant(
            tenant_id=uuid.uuid4(),
            legal_name="K",
            display_name="K",
            currency="ARS",
            pricing_reference_mode="MEP",
            status="ACTIVE",
        )
        session.add(tenant)
        await session.commit()

        summary = _multisheet_product_summary(
            [
                {"producto": "Fanta 500ml", "precio": "1000", "costo": "600", "stock": "5"},
                {"producto": "Fanta 500ml", "precio": "1000", "costo": "600", "stock": "3"},
            ]
        )
        counts = await importer.insert_confirmed_data(
            session,
            tenant.tenant_id,
            summary,
            {"productos": True},
            context_confirmed={"sheet:Productos": True},
        )
        await session.commit()

        assert counts["productos"] == 2
        assert counts["productos_ambiguos"] == 0

        products = (
            await session.execute(
                select(Product).where(Product.tenant_id == tenant.tenant_id)
            )
        ).scalars().all()
        assert len(products) == 1
        assert products[0].stock_units == 3


# ── 3. count==0 crea / count==1 sigue actualizando (no debe romper con el helper) ──


@pytest.mark.asyncio
async def test_single_sheet_count_one_still_updates(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Con exactamente 1 producto existente (no ambiguo), el helper tolerante
    sigue actualizando ese producto como antes."""
    existing = Product(
        id=uuid.uuid4(),
        tenant_id=sample_tenant.tenant_id,
        name="Agua 500ml",
        sale_price_ars=Decimal("500"),
        unit_cost_ars=Decimal("300"),
        stock_units=2,
        is_active=True,
    )
    db_session.add(existing)
    await db_session.commit()

    summary = _stock_summary(
        [{"producto": "Agua 500ml", "precio": "600", "costo": "350", "stock": "20"}]
    )
    counts = await importer.insert_confirmed_data(
        db_session, sample_tenant.tenant_id, summary, {"productos": True}
    )

    assert counts["productos"] == 1
    assert counts["productos_ambiguos"] == 0

    products = (
        await db_session.execute(
            select(Product).where(Product.tenant_id == sample_tenant.tenant_id)
        )
    ).scalars().all()
    assert len(products) == 1
    assert products[0].id == existing.id
    assert products[0].sale_price_ars == Decimal("600")
    assert products[0].stock_units == 20


@pytest.mark.asyncio
async def test_multisheet_new_product_created_when_no_match(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """count==0 (sin producto preexistente) sigue creando, también en el path
    multisheet."""
    summary = _multisheet_product_summary(
        [{"producto": "Alfajor Nuevo", "precio": "800", "costo": "400", "stock": "10"}]
    )
    counts = await importer.insert_confirmed_data(
        db_session,
        sample_tenant.tenant_id,
        summary,
        {"productos": True},
        context_confirmed={"sheet:Productos": True},
    )

    assert counts["productos"] == 1
    assert counts["productos_ambiguos"] == 0

    product = (
        await db_session.execute(
            select(Product).where(Product.tenant_id == sample_tenant.tenant_id)
        )
    ).scalar_one()
    assert product.name == "Alfajor Nuevo"
    assert product.stock_units == 10
