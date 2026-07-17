"""F2-T2: resolución de identidad de producto por claves independientes.

F1 dejó un lookup de producto SOLO por nombre (``_find_product_by_name_tolerant``
+ ``_load_product_name_lookup_indexes``, ahora eliminados — ver
``test_ingestion_product_name_collision.py`` para la historia). T1 (F2) agregó
las columnas ``*_normalized`` persistidas (listener ``before_insert``/
``before_update`` en ``app/persistence/models/product.py``) y
``unclassified_records.match_candidates``.

T2 reemplaza la resolución name-only por resolución de IDENTIDAD por claves
INDEPENDIENTES (no jerárquica excluyente): orden barcode → sku → nombre+marca,
con detección de AMBIGÜEDAD (≥2 candidatos en un mismo tier) y CONFLICTO
(tiers distintos que apuntan a productos DISTINTOS) — ambos ruteados a "Otros"
con ``match_candidates``. El import de archivos no parsea barcode todavía (fase
posterior), así que en la práctica el import resuelve por sku → nombre+marca.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import app.application.services.ingestion_import_service as importer
from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant
from app.persistence.models.unclassified_record import (
    UNCLASSIFIED_STATUS_PENDING,
    UnclassifiedRecord,
)


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
                "headers": ["producto", "precio", "costo", "stock", "sku", "tienda"],
                "fields": None,
                "preview_rows": [],
                "row_count": len(rows),
            }
        ],
        "ventas_detectadas": [],
        "gastos_detectados": [],
        "stock_detectado": [{**row, "__context__": "sheet:Productos"} for row in rows],
    }


async def _create_product(
    db_session: AsyncSession,
    tenant_id: uuid.UUID,
    name: str,
    *,
    sku: str | None = None,
    marca: str | None = None,
    is_active: bool = True,
    sale_price_ars: Decimal = Decimal("1000"),
    unit_cost_ars: Decimal = Decimal("600"),
    stock_units: int = 5,
) -> Product:
    """Producto preexistente, con soporte opcional de sku/marca (custom_fields)
    para que el listener de T1 pueble ``sku_normalized``/``brand_normalized``.
    """
    extra: dict[str, Any] = {}
    if marca:
        extra["custom_fields"] = {"marca": marca}
    product = Product(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        name=name,
        sku=sku,
        sale_price_ars=sale_price_ars,
        unit_cost_ars=unit_cost_ars,
        stock_units=stock_units,
        is_active=is_active,
        **extra,
    )
    db_session.add(product)
    await db_session.commit()
    await db_session.refresh(product)
    return product


async def _all_products(db_session: AsyncSession, tenant_id: uuid.UUID) -> list[Product]:
    result = await db_session.execute(select(Product).where(Product.tenant_id == tenant_id))
    return list(result.scalars().all())


async def _all_unclassified(
    db_session: AsyncSession, tenant_id: uuid.UUID
) -> list[UnclassifiedRecord]:
    result = await db_session.execute(
        select(UnclassifiedRecord).where(UnclassifiedRecord.tenant_id == tenant_id)
    )
    return list(result.scalars().all())


# ── 1. SKU desambigua nombre repetido ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_sku_disambiguates_repeated_name(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """2 productos activos "Agua" con SKU distinto ("A1"/"A2"): una fila con
    ``name="Agua", sku="A1"`` debe resolver al de SKU "A1" — NO ambiguo, NO
    crea un tercero — aunque el nombre por sí solo colisione."""
    p1 = await _create_product(db_session, sample_tenant.tenant_id, "Agua", sku="A1")
    p2 = await _create_product(db_session, sample_tenant.tenant_id, "Agua", sku="A2")

    summary = _stock_summary(
        [{"producto": "Agua", "sku": "A1", "precio": "1500", "costo": "900", "stock": "50"}]
    )
    counts = await importer.insert_confirmed_data(
        db_session, sample_tenant.tenant_id, summary, {"productos": True}
    )

    assert counts["productos"] == 1
    assert counts["otros"] == 0
    assert counts["productos_ambiguos"] == 0

    products = await _all_products(db_session, sample_tenant.tenant_id)
    assert len(products) == 2  # no se creó un tercero
    by_id = {p.id: p for p in products}
    assert by_id[p1.id].sale_price_ars == Decimal("1500")
    assert by_id[p1.id].stock_units == 50
    # El otro "Agua" (sku A2) queda intacto.
    assert by_id[p2.id].sale_price_ars == Decimal("1000")
    assert by_id[p2.id].stock_units == 5


# ── 2. Marca desambigua nombre repetido ────────────────────────────────────────


@pytest.mark.asyncio
async def test_brand_disambiguates_repeated_name(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """2 productos "Agua" con marca "X"/"Y" (``custom_fields.marca``, poblado
    ``brand_normalized`` por el listener de T1): fila ``name="Agua",
    tienda="X"`` (sin sku) resuelve al producto de marca X."""
    p_x = await _create_product(db_session, sample_tenant.tenant_id, "Agua", marca="X")
    p_y = await _create_product(db_session, sample_tenant.tenant_id, "Agua", marca="Y")

    summary = _stock_summary(
        [{"producto": "Agua", "tienda": "X", "precio": "1500", "costo": "900", "stock": "50"}]
    )
    counts = await importer.insert_confirmed_data(
        db_session, sample_tenant.tenant_id, summary, {"productos": True}
    )

    assert counts["productos"] == 1
    assert counts["otros"] == 0
    assert counts["productos_ambiguos"] == 0

    products = await _all_products(db_session, sample_tenant.tenant_id)
    assert len(products) == 2
    by_id = {p.id: p for p in products}
    assert by_id[p_x.id].sale_price_ars == Decimal("1500")
    assert by_id[p_x.id].stock_units == 50
    assert by_id[p_y.id].sale_price_ars == Decimal("1000")  # intacto


# ── 3. Ambiguo sin desambiguador ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ambiguous_without_disambiguator_goes_to_otros(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """2 productos "Agua" sin sku ni marca + fila sin sku/marca en la fila →
    ``status=ambiguous``: NO crea, NO actualiza, NO explota (500) — la fila
    queda en ``unclassified_records`` con ``suggested_entity="product"`` y
    ``match_candidates`` con 2 entradas."""
    p1 = await _create_product(db_session, sample_tenant.tenant_id, "Agua")
    p2 = await _create_product(db_session, sample_tenant.tenant_id, "Agua")

    summary = _stock_summary(
        [{"producto": "Agua", "precio": "1500", "costo": "900", "stock": "50"}]
    )
    counts = await importer.insert_confirmed_data(
        db_session, sample_tenant.tenant_id, summary, {"productos": True}
    )

    assert counts["productos"] == 0
    assert counts["otros"] == 1
    assert counts["productos_ambiguos"] == 1

    # Ningún producto existente fue tocado.
    products = await _all_products(db_session, sample_tenant.tenant_id)
    by_id = {p.id: p for p in products}
    assert by_id[p1.id].sale_price_ars == Decimal("1000")
    assert by_id[p2.id].sale_price_ars == Decimal("1000")

    records = await _all_unclassified(db_session, sample_tenant.tenant_id)
    assert len(records) == 1
    record = records[0]
    assert record.status == UNCLASSIFIED_STATUS_PENDING
    assert record.suggested_entity == "product"
    assert "coincide con 2 productos activos" in (record.context_label or "")
    assert record.match_candidates is not None
    assert len(record.match_candidates) == 2
    candidate_ids = {c["id"] for c in record.match_candidates}
    assert candidate_ids == {str(p1.id), str(p2.id)}
    for candidate in record.match_candidates:
        assert candidate["matched_by"] == ["name"]


# ── 4. Conflicto: sku y nombre+marca apuntan a productos DISTINTOS ────────────


@pytest.mark.asyncio
async def test_conflict_sku_and_name_brand_point_to_different_products(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Producto A (sku="S1", name="Coca", sin marca) + producto B (name="Coca",
    marca="Andina", sin sku). Fila ``name="Coca", sku="S1", tienda="Andina"``:
    el tier sku resuelve a A, el tier nombre+marca resuelve a B → CONFLICTO
    (no ambigüedad de un mismo tier) → Otros con ambos candidatos, ninguno se
    toca."""
    product_a = await _create_product(db_session, sample_tenant.tenant_id, "Coca", sku="S1")
    product_b = await _create_product(
        db_session, sample_tenant.tenant_id, "Coca", marca="Andina"
    )

    summary = _stock_summary(
        [
            {
                "producto": "Coca",
                "sku": "S1",
                "tienda": "Andina",
                "precio": "1500",
                "costo": "900",
                "stock": "50",
            }
        ]
    )
    counts = await importer.insert_confirmed_data(
        db_session, sample_tenant.tenant_id, summary, {"productos": True}
    )

    assert counts["productos"] == 0
    assert counts["otros"] == 1
    assert counts["productos_ambiguos"] == 1

    products = await _all_products(db_session, sample_tenant.tenant_id)
    by_id = {p.id: p for p in products}
    assert by_id[product_a.id].sale_price_ars == Decimal("1000")  # intacto
    assert by_id[product_b.id].sale_price_ars == Decimal("1000")  # intacto

    records = await _all_unclassified(db_session, sample_tenant.tenant_id)
    assert len(records) == 1
    record = records[0]
    assert record.suggested_entity == "product"
    assert record.context_label == (
        "Conflicto de identidad: el SKU y el nombre apuntan a productos distintos"
    )
    assert record.match_candidates is not None
    assert len(record.match_candidates) == 2
    by_candidate_id = {c["id"]: c for c in record.match_candidates}
    assert set(by_candidate_id[str(product_a.id)]["matched_by"]) == {"sku"}
    assert set(by_candidate_id[str(product_b.id)]["matched_by"]) == {"name", "brand"}


# ── 5. Create + caché intra-corrida (camino multisheet) ───────────────────────


@pytest.mark.asyncio
async def test_create_new_product_and_second_identical_row_uses_cache(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Sin producto preexistente: la primera fila crea; una 2da fila IDÉNTICA
    (mismo nombre+marca, mismo archivo) usa la caché intra-corrida en vez de
    volver a consultar el motor → 1 solo producto creado (camino multisheet
    ``_add_product``)."""
    summary = _multisheet_product_summary(
        [
            {
                "producto": "Alfajor Nuevo",
                "tienda": "MarcaZ",
                "precio": "800",
                "costo": "400",
                "stock": "10",
            },
            {
                "producto": "Alfajor Nuevo",
                "tienda": "MarcaZ",
                "precio": "800",
                "costo": "400",
                "stock": "3",
            },
        ]
    )
    counts = await importer.insert_confirmed_data(
        db_session,
        sample_tenant.tenant_id,
        summary,
        {"productos": True},
        context_confirmed={"sheet:Productos": True},
    )

    assert counts["productos"] == 2  # ambas filas procesadas (create + update vía caché)
    assert counts["otros"] == 0
    assert counts["productos_ambiguos"] == 0

    products = await _all_products(db_session, sample_tenant.tenant_id)
    assert len(products) == 1
    assert products[0].name == "Alfajor Nuevo"
    assert products[0].stock_units == 3  # la 2da fila actualizó el mismo producto


# ── 6. Acentos: el name_normalized ignora diacríticos ──────────────────────────


@pytest.mark.asyncio
async def test_accented_name_matches_existing_no_duplicate(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Producto "Café" existente + fila "Cafe" (sin acento, sin marca): el
    tier nombre matchea vía ``name_normalized`` (quita diacríticos) → NO
    duplica, actualiza el existente."""
    existing = await _create_product(db_session, sample_tenant.tenant_id, "Café")

    summary = _stock_summary(
        [{"producto": "Cafe", "precio": "1500", "costo": "900", "stock": "50"}]
    )
    counts = await importer.insert_confirmed_data(
        db_session, sample_tenant.tenant_id, summary, {"productos": True}
    )

    assert counts["productos"] == 1
    assert counts["otros"] == 0
    assert counts["productos_ambiguos"] == 0

    products = await _all_products(db_session, sample_tenant.tenant_id)
    assert len(products) == 1
    assert products[0].id == existing.id
    assert products[0].sale_price_ars == Decimal("1500")
    assert products[0].stock_units == 50


# ── 7. Aislamiento entre tenants ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_other_tenant_product_is_not_a_resolution_candidate(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Un producto con el mismo SKU/nombre en OTRO tenant no debe participar
    de la resolución: la fila crea un producto NUEVO en el tenant bajo test,
    sin tocar el del otro tenant."""
    other_tenant = Tenant(
        tenant_id=uuid.uuid4(),
        legal_name="Otro Kiosco",
        display_name="Otro Kiosco",
        currency="ARS",
        pricing_reference_mode="MEP",
        status="ACTIVE",
    )
    db_session.add(other_tenant)
    await db_session.commit()
    other_product = await _create_product(
        db_session, other_tenant.tenant_id, "Agua", sku="A1"
    )

    summary = _stock_summary(
        [{"producto": "Agua", "sku": "A1", "precio": "1500", "costo": "900", "stock": "50"}]
    )
    counts = await importer.insert_confirmed_data(
        db_session, sample_tenant.tenant_id, summary, {"productos": True}
    )

    assert counts["productos"] == 1
    assert counts["otros"] == 0
    assert counts["productos_ambiguos"] == 0

    tenant_products = await _all_products(db_session, sample_tenant.tenant_id)
    assert len(tenant_products) == 1
    assert tenant_products[0].id != other_product.id
    assert tenant_products[0].sale_price_ars == Decimal("1500")

    other_tenant_products = await _all_products(db_session, other_tenant.tenant_id)
    assert len(other_tenant_products) == 1
    assert other_tenant_products[0].sale_price_ars == Decimal("1000")  # intacto


# ── 8. Forma de match_candidates persistido ────────────────────────────────────


@pytest.mark.asyncio
async def test_match_candidates_persisted_shape(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """``unclassified_records.match_candidates`` tiene la forma
    ``{id, matched_by, name, sku, barcode}`` por candidato — verificado sobre
    el escenario de conflicto (matched_by con más de un valor)."""
    product_a = await _create_product(db_session, sample_tenant.tenant_id, "Coca", sku="S1")
    product_b = await _create_product(
        db_session, sample_tenant.tenant_id, "Coca", marca="Andina"
    )

    summary = _stock_summary(
        [
            {
                "producto": "Coca",
                "sku": "S1",
                "tienda": "Andina",
                "precio": "1500",
                "costo": "900",
                "stock": "50",
            }
        ]
    )
    await importer.insert_confirmed_data(
        db_session, sample_tenant.tenant_id, summary, {"productos": True}
    )

    records = await _all_unclassified(db_session, sample_tenant.tenant_id)
    assert len(records) == 1
    candidates = records[0].match_candidates
    assert candidates is not None
    assert len(candidates) == 2
    for candidate in candidates:
        assert set(candidate.keys()) == {"id", "matched_by", "name", "sku", "barcode"}
        assert isinstance(candidate["matched_by"], list)

    by_id = {c["id"]: c for c in candidates}
    assert by_id[str(product_a.id)]["name"] == "Coca"
    assert by_id[str(product_a.id)]["sku"] == "S1"
    assert by_id[str(product_a.id)]["barcode"] is None
    assert by_id[str(product_b.id)]["name"] == "Coca"
    assert by_id[str(product_b.id)]["sku"] is None
