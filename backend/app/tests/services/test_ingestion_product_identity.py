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


# ── FIX A (Crítico, review de T2) ───────────────────────────────────────────
# La caché intra-corrida de UNA sola clave (``sku:{sku_n}`` si hay sku, si no
# ``nb:{name_n}|{brand_n}``) duplicaba productos cuando 2 filas del MISMO
# archivo son el MISMO producto lógico pero difieren en si traen SKU: fila1
# "Fideos" con sku="X1" registra ``sku:x1``; fila2 "Fideos" sin sku busca
# ``nb:fideos|`` → miss → crea un SEGUNDO producto. La caché ahora es
# multi-clave: registra el producto resuelto/creado bajo TODAS sus claves
# aplicables y busca por las claves de la fila en el mismo orden de
# prioridad del motor (sku → nombre+marca → nombre), cayendo a name-only
# SOLO cuando la fila no trae marca (misma semántica que el motor).


@pytest.mark.asyncio
async def test_single_sheet_sku_then_no_sku_same_run_creates_one_product(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """fila1 ``sku="X1"`` + fila2 SIN sku, mismo nombre, sin marca, sin
    producto preexistente (camino single-sheet in-place) → 1 solo producto."""
    summary = _stock_summary(
        [
            {
                "producto": "Fideos",
                "sku": "X1",
                "precio": "1000",
                "costo": "600",
                "stock": "5",
            },
            {"producto": "Fideos", "precio": "1000", "costo": "600", "stock": "3"},
        ]
    )
    counts = await importer.insert_confirmed_data(
        db_session, sample_tenant.tenant_id, summary, {"productos": True}
    )

    assert counts["productos"] == 2  # ambas filas procesadas (create + update)
    assert counts["otros"] == 0
    assert counts["productos_ambiguos"] == 0

    products = await _all_products(db_session, sample_tenant.tenant_id)
    assert len(products) == 1  # NO se duplicó
    assert products[0].sku == "X1"
    assert products[0].stock_units == 3  # la 2da fila (sin sku) actualizó el mismo


@pytest.mark.asyncio
async def test_single_sheet_no_sku_then_sku_same_run_creates_one_product(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Caso inverso: fila1 SIN sku + fila2 CON sku, mismo nombre → 1 producto.

    La detección de columnas de la hoja usa las keys de la PRIMERA fila
    (``headers = list(rows[0].keys())`` — limitación preexistente, no parte
    de este fix): fila1 incluye la columna "sku" con valor vacío para que se
    detecte igual, replicando una planilla real (columna presente, celda en
    blanco para ese producto puntual).
    """
    summary = _stock_summary(
        [
            {
                "producto": "Fideos",
                "sku": "",
                "precio": "1000",
                "costo": "600",
                "stock": "5",
            },
            {
                "producto": "Fideos",
                "sku": "X1",
                "precio": "1000",
                "costo": "600",
                "stock": "3",
            },
        ]
    )
    counts = await importer.insert_confirmed_data(
        db_session, sample_tenant.tenant_id, summary, {"productos": True}
    )

    assert counts["productos"] == 2
    assert counts["otros"] == 0
    assert counts["productos_ambiguos"] == 0

    products = await _all_products(db_session, sample_tenant.tenant_id)
    assert len(products) == 1
    assert products[0].sku == "X1"
    assert products[0].stock_units == 3


@pytest.mark.asyncio
async def test_multisheet_sku_then_no_sku_same_run_creates_one_product(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Mismo bug, camino multisheet (``_add_product``) — el otro call site
    que consume la caché de identidad."""
    summary = _multisheet_product_summary(
        [
            {
                "producto": "Fideos",
                "sku": "X1",
                "precio": "1000",
                "costo": "600",
                "stock": "5",
            },
            {"producto": "Fideos", "precio": "1000", "costo": "600", "stock": "3"},
        ]
    )
    counts = await importer.insert_confirmed_data(
        db_session,
        sample_tenant.tenant_id,
        summary,
        {"productos": True},
        context_confirmed={"sheet:Productos": True},
    )

    assert counts["productos"] == 2
    assert counts["otros"] == 0
    assert counts["productos_ambiguos"] == 0

    products = await _all_products(db_session, sample_tenant.tenant_id)
    assert len(products) == 1
    assert products[0].sku == "X1"
    assert products[0].stock_units == 3


@pytest.mark.asyncio
async def test_single_sheet_two_brands_same_name_same_run_creates_two_products(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Regresión (el OJO del fix): 2 filas del MISMO archivo, mismo nombre
    pero DISTINTA marca, sin producto preexistente → siguen creando 2
    productos. La caché name-only NO debe fusionarlas: el lookup de una fila
    CON marca nunca cae al tier name-only (misma restricción que el motor)."""
    summary = _stock_summary(
        [
            {
                "producto": "Agua",
                "tienda": "MarcaX",
                "precio": "1000",
                "costo": "600",
                "stock": "5",
            },
            {
                "producto": "Agua",
                "tienda": "MarcaY",
                "precio": "1200",
                "costo": "700",
                "stock": "8",
            },
        ]
    )
    counts = await importer.insert_confirmed_data(
        db_session, sample_tenant.tenant_id, summary, {"productos": True}
    )

    assert counts["productos"] == 2
    assert counts["otros"] == 0
    assert counts["productos_ambiguos"] == 0

    products = await _all_products(db_session, sample_tenant.tenant_id)
    assert len(products) == 2  # NO se fusionaron
    assert sorted(p.stock_units for p in products) == [5, 8]


# ── FIX B (Important, review de T2) ─────────────────────────────────────────
# ``match_candidates`` se armaba desde ``order`` (la unión de TODOS los ids
# vistos en cualquier tier) en vez del conjunto final post-intersección
# (``candidate_set``) — un id que un tier posterior descartó por narrowing
# seguía apareciendo en ``match_candidates``, confundiendo la revisión manual
# en /otros.


@pytest.mark.asyncio
@pytest.mark.usefixtures("legacy_pre_f5_schema")
async def test_ambiguous_candidates_exclude_id_narrowed_out_by_other_tier(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """3 productos comparten el SKU "S1" (A, B, C) — el tier sku por sí solo
    es ambiguo con 3 candidatos — pero el tier nombre+marca de la fila
    (marca "MarcaX") solo matchea a A y B (C tiene otra marca): la
    intersección deja el ``candidate_set`` final en {A, B}. C fue descartado
    por narrowing y NO debe aparecer en ``match_candidates``."""
    prod_a = await _create_product(
        db_session, sample_tenant.tenant_id, "Fideos", sku="S1", marca="MarcaX"
    )
    prod_b = await _create_product(
        db_session, sample_tenant.tenant_id, "Fideos", sku="S1", marca="MarcaX"
    )
    prod_c = await _create_product(
        db_session, sample_tenant.tenant_id, "Fideos", sku="S1", marca="OtraMarca"
    )

    indexes = await importer._load_product_identity_indexes(
        db_session, sample_tenant.tenant_id
    )
    resolution = importer._resolve_product_identity(
        "Fideos", "S1", "MarcaX", indexes=indexes
    )

    assert resolution.status == "ambiguous"
    assert resolution.product_id is None
    candidate_ids = {c["id"] for c in resolution.candidates}
    assert candidate_ids == {str(prod_a.id), str(prod_b.id)}
    assert str(prod_c.id) not in candidate_ids  # descartado por narrowing


@pytest.mark.asyncio
@pytest.mark.usefixtures("legacy_pre_f5_schema")
async def test_conflict_candidates_are_exactly_the_conflicting_set(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Test de regresión/blindaje para status=conflict tras el refactor de
    FIX B: con 2 tiers activos (barcode ambiguo [A, B] + sku disjunto [C],
    sin tier de nombre) el conjunto de ``match_candidates`` debe seguir
    siendo EXACTAMENTE {A, B, C} — ninguno de por medio se pierde."""
    prod_a = Product(
        id=uuid.uuid4(),
        tenant_id=sample_tenant.tenant_id,
        name="Gaseosa",
        barcode="77900010",
        sale_price_ars=Decimal("1000"),
        unit_cost_ars=Decimal("600"),
        stock_units=5,
    )
    prod_b = Product(
        id=uuid.uuid4(),
        tenant_id=sample_tenant.tenant_id,
        name="Gaseosa Light",
        barcode="77900010",
        sale_price_ars=Decimal("1000"),
        unit_cost_ars=Decimal("600"),
        stock_units=5,
    )
    prod_c = Product(
        id=uuid.uuid4(),
        tenant_id=sample_tenant.tenant_id,
        name="Otra Gaseosa",
        sku="S9",
        sale_price_ars=Decimal("1000"),
        unit_cost_ars=Decimal("600"),
        stock_units=5,
    )
    db_session.add_all([prod_a, prod_b, prod_c])
    await db_session.commit()

    indexes = await importer._load_product_identity_indexes(
        db_session, sample_tenant.tenant_id
    )
    resolution = importer._resolve_product_identity(
        None, "S9", None, indexes=indexes, barcode="77900010"
    )

    assert resolution.status == "conflict"
    assert resolution.product_id is None
    candidate_ids = {c["id"] for c in resolution.candidates}
    assert candidate_ids == {str(prod_a.id), str(prod_b.id), str(prod_c.id)}


# ── F2 review — casos peligrosos del deploy-gate ────────────────────────────────


@pytest.mark.asyncio
async def test_legacy_null_normalized_still_resolves(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """BLOQUEANTE 1 del review: un producto LEGACY (columnas ``*_normalized`` en
    NULL, previo al listener de T1) debe seguir siendo matcheable — el loader
    computa la clave on-the-fly. Sin esto, el import lo daría por inexistente y
    crearía un duplicado.
    """
    from sqlalchemy import text  # noqa: PLC0415

    p = await _create_product(db_session, sample_tenant.tenant_id, "Yerba Playadito")
    p_id = p.id  # capturar ANTES de expirar (evita lazy-load sync post expire_all)
    # Simular producto legacy: NULLear las columnas normalizadas por SQL crudo
    # (bypassa el listener ORM, que solo dispara en insert/update de entidad).
    await db_session.execute(
        text(
            "UPDATE products SET name_normalized=NULL, sku_normalized=NULL, "
            "barcode_normalized=NULL, brand_normalized=NULL WHERE id=:id"
        ),
        {"id": str(p_id)},
    )
    await db_session.commit()

    summary = _stock_summary(
        [{"producto": "Yerba Playadito", "precio": "2500", "costo": "1500", "stock": "30"}]
    )
    counts = await importer.insert_confirmed_data(
        db_session, sample_tenant.tenant_id, summary, {"productos": True}
    )

    assert counts["productos"] == 1
    assert counts["otros"] == 0
    products = await _all_products(db_session, sample_tenant.tenant_id)
    assert len(products) == 1  # actualizó el legacy, NO creó un duplicado
    assert products[0].id == p_id
    assert products[0].sale_price_ars == Decimal("2500")


@pytest.mark.asyncio
async def test_barcode_from_file_creates_and_resolves(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """F2-T5: el import parsea la columna de código de barras, la setea en el
    producto creado, y una fila POSTERIOR con el MISMO barcode pero nombre
    distinto resuelve por barcode (no crea un duplicado)."""
    summary1 = _stock_summary(
        [{"producto": "Coca Cola", "ean": "7790895000123", "precio": "1500",
          "costo": "900", "stock": "40"}]
    )
    counts1 = await importer.insert_confirmed_data(
        db_session, sample_tenant.tenant_id, summary1, {"productos": True}
    )
    assert counts1["productos"] == 1
    products = await _all_products(db_session, sample_tenant.tenant_id)
    assert len(products) == 1
    assert products[0].barcode == "7790895000123"
    assert products[0].barcode_normalized == "7790895000123"

    # Segunda corrida: mismo barcode, nombre DISTINTO → matchea por barcode.
    summary2 = _stock_summary(
        [{"producto": "Coca-Cola 500", "ean": "7790895000123", "precio": "1600",
          "costo": "950", "stock": "10"}]
    )
    counts2 = await importer.insert_confirmed_data(
        db_session, sample_tenant.tenant_id, summary2, {"productos": True}
    )
    assert counts2["productos"] == 1
    assert counts2["otros"] == 0
    products = await _all_products(db_session, sample_tenant.tenant_id)
    assert len(products) == 1  # resolvió por barcode, no duplicó
    assert products[0].sale_price_ars == Decimal("1600")


@pytest.mark.asyncio
async def test_partial_sku_rows_same_file_no_duplicate(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """BLOQUEANTE 2 del review (caché intra-corrida): dos filas del MISMO archivo
    son el mismo producto lógico pero difieren en si traen SKU — fila1 con sku,
    fila2 sin sku. NO debe crear dos productos. Y en el orden inverso también."""
    summary = _stock_summary(
        [
            {"producto": "Fideos Matarazzo", "sku": "F1", "precio": "800", "stock": "5"},
            {"producto": "Fideos Matarazzo", "precio": "800", "stock": "7"},
        ]
    )
    counts = await importer.insert_confirmed_data(
        db_session, sample_tenant.tenant_id, summary, {"productos": True}
    )
    assert counts["otros"] == 0
    products = await _all_products(db_session, sample_tenant.tenant_id)
    assert len(products) == 1  # una sola identidad pese al sku parcial


@pytest.mark.asyncio
async def test_partial_sku_rows_reverse_order_no_duplicate(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Orden inverso del anterior: fila1 SIN sku, fila2 CON sku → 1 producto."""
    summary = _stock_summary(
        [
            {"producto": "Fideos Matarazzo", "precio": "800", "stock": "7"},
            {"producto": "Fideos Matarazzo", "sku": "F1", "precio": "800", "stock": "5"},
        ]
    )
    counts = await importer.insert_confirmed_data(
        db_session, sample_tenant.tenant_id, summary, {"productos": True}
    )
    assert counts["otros"] == 0
    products = await _all_products(db_session, sample_tenant.tenant_id)
    assert len(products) == 1


@pytest.mark.asyncio
async def test_resolve_product_link_is_accent_tolerant(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Finding 4 (unificación de resolución): el link de ventas/gastos/compras
    (``_resolve_product`` + ``_load_product_index``) ahora usa la MISMA
    normalización canónica que el bucket de productos — "Cafe Molido" (venta)
    matchea "Café Molido" (catálogo) pese al acento. Antes divergían."""
    p = await _create_product(db_session, sample_tenant.tenant_id, "Café Molido", sku="CM1")
    by_sku, by_name, by_token = await importer._load_product_index(
        db_session, sample_tenant.tenant_id
    )
    # Match por nombre acentuado-vs-sin-acento.
    assert importer._resolve_product(by_sku, by_name, "Cafe Molido", None, by_token) == p.id
    # Match por sku con casing/espacios distintos (normalize_sku canónico).
    assert importer._resolve_product(by_sku, by_name, None, " cm1 ", by_token) == p.id


# ── F2 review ronda 2 — compras ambiguas + link por barcode ─────────────────────


@pytest.mark.asyncio
async def test_ambiguous_purchase_goes_to_otros_not_new_product(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Review F2 #1: una compra de mercadería cuyo producto es AMBIGUO (nombre que
    coincide con ≥2 del catálogo, sin sku/barcode que desambigüe) NO debe crear un
    3er producto — la fila va a "Otros" (con match_candidates) y el gasto NO se
    registra. Antes ``_resolve_product`` devolvía None → se interpretaba "no existe"
    → se creaba un duplicado (2 productos → 3)."""
    p1 = await _create_product(db_session, sample_tenant.tenant_id, "Fideos")
    p2 = await _create_product(db_session, sample_tenant.tenant_id, "Fideos")

    summary = {
        "file_type": "spreadsheet",
        "inferred_type": "gastos",
        "has_gasto": True,
        "gastos_detectados": [
            {
                "fecha": "2024-02-01",
                "categoria": "mercaderia",
                "producto": "Fideos",  # ambiguo: coincide con p1 y p2
                "cantidad": "10",
                "monto": "5000",
                "costo_unitario": "500",
                "forma_pago": "efectivo",
            },
        ],
    }
    counts = await importer.insert_confirmed_data(
        db_session, sample_tenant.tenant_id, summary, {"gastos": True}
    )

    assert counts["gastos"] == 0  # el gasto NO se registró
    assert counts["otros"] == 1
    products = await _all_products(db_session, sample_tenant.tenant_id)
    assert len(products) == 2  # NO se creó un 3er producto
    records = await _all_unclassified(db_session, sample_tenant.tenant_id)
    assert len(records) == 1
    assert records[0].suggested_entity == "expense"
    assert records[0].match_candidates is not None
    assert {c["id"] for c in records[0].match_candidates} == {str(p1.id), str(p2.id)}


@pytest.mark.asyncio
async def test_sale_links_by_barcode(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Review F2 #5: una venta con columna de código de barras linkea al producto
    por barcode aunque el nombre difiera (el barcode es el identificador más fuerte)."""
    from sqlalchemy import select as _select  # noqa: PLC0415

    from app.persistence.models.transaction import SaleEntry  # noqa: PLC0415

    p = Product(
        id=uuid.uuid4(),
        tenant_id=sample_tenant.tenant_id,
        name="Coca Cola",
        barcode="7790895000123",
        sale_price_ars=Decimal("1500"),
        unit_cost_ars=Decimal("900"),
        stock_units=10,
    )
    db_session.add(p)
    await db_session.commit()
    p_id = p.id

    summary = {
        "file_type": "spreadsheet",
        "inferred_type": "ventas",
        "has_venta": True,
        "ventas_detectadas": [
            # nombre distinto ("Gaseosa cola"), pero mismo EAN → linkea por barcode.
            {"fecha": "2024-01-15", "monto": "1500", "producto": "Gaseosa cola",
             "ean": "7790895000123"},
        ],
    }
    await importer.insert_confirmed_data(
        db_session, sample_tenant.tenant_id, summary, {"ventas": True}
    )

    sale = (await db_session.execute(_select(SaleEntry))).scalar_one()
    assert sale.product_id == p_id


# ── F2 review ronda 3 — fixes de compras ambiguas / índices / idempotencia ──────


@pytest.mark.asyncio
async def test_ambiguous_purchase_mixed_file_captured_to_otros_once(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Review F2 #1: archivo 'general' con gastos Y productos sobre las MISMAS filas.
    Una compra ambigua se captura a Otros en el bloque de gastos y la fila queda
    marcada (`_captured_to_otros_rows`) para que el bucket de productos NO la
    recapture — UN solo UnclassifiedRecord, no dos."""
    await _create_product(db_session, sample_tenant.tenant_id, "Fideos")
    await _create_product(db_session, sample_tenant.tenant_id, "Fideos")

    summary = {
        "file_type": "spreadsheet",
        "inferred_type": "general",
        "has_gasto": True,
        "has_producto": True,
        "gastos_detectados": [
            {
                "fecha": "2024-02-01",
                "categoria": "mercaderia",
                "producto": "Fideos",
                "cantidad": "10",
                "monto": "5000",
                "costo_unitario": "500",
                "forma_pago": "efectivo",
            },
        ],
    }
    counts = await importer.insert_confirmed_data(
        db_session, sample_tenant.tenant_id, summary, {"gastos": True, "productos": True}
    )

    assert counts["otros"] == 1  # UNA captura, no dos
    products = await _all_products(db_session, sample_tenant.tenant_id)
    assert len(products) == 2  # no se creó un 3er producto
    records = await _all_unclassified(db_session, sample_tenant.tenant_id)
    assert len(records) == 1


@pytest.mark.asyncio
async def test_repeated_purchase_new_product_counts_created_once(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Review F2 #2/#3: 2 filas de compra del MISMO producto nuevo. La fila2 debe
    LINKEAR al producto que creó la fila1 vía `_resolve_product` (registrado en los
    índices transaccionales por el fix #2) — un solo producto — y `sin_producto`
    debe contar 1 (solo la creación), no 2 (el link de la fila2 no cuenta, #3)."""
    summary = {
        "file_type": "spreadsheet",
        "inferred_type": "gastos",
        "has_gasto": True,
        "gastos_detectados": [
            {"fecha": "2024-02-01", "categoria": "mercaderia", "producto": "Yerba Nueva",
             "cantidad": "5", "monto": "2500", "costo_unitario": "500", "forma_pago": "efectivo"},
            {"fecha": "2024-02-02", "categoria": "mercaderia", "producto": "Yerba Nueva",
             "cantidad": "3", "monto": "1500", "costo_unitario": "500", "forma_pago": "efectivo"},
        ],
    }
    counts = await importer.insert_confirmed_data(
        db_session, sample_tenant.tenant_id, summary, {"gastos": True}
    )

    products = await _all_products(db_session, sample_tenant.tenant_id)
    assert len(products) == 1  # una sola creación
    assert counts["sin_producto"] == 1  # #3: el link de la fila2 no cuenta
    assert counts["gastos"] == 2  # ambos gastos registrados


@pytest.mark.asyncio
async def test_ambiguous_purchase_idempotent_on_reimport(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Review F2 #6: re-subir el MISMO archivo (mismo uploaded_file_id) con una
    compra ambigua NO re-crea el UnclassifiedRecord — la captura a Otros registra
    fingerprint de fila, así que la 2ª corrida la saltea por idempotencia."""
    await _create_product(db_session, sample_tenant.tenant_id, "Fideos")
    await _create_product(db_session, sample_tenant.tenant_id, "Fideos")
    upload_id = uuid.uuid4()
    summary = {
        "file_type": "spreadsheet",
        "inferred_type": "gastos",
        "has_gasto": True,
        "gastos_detectados": [
            {"fecha": "2024-02-01", "categoria": "mercaderia", "producto": "Fideos",
             "cantidad": "10", "monto": "5000", "costo_unitario": "500", "forma_pago": "efectivo"},
        ],
    }
    counts1 = await importer.insert_confirmed_data(
        db_session, sample_tenant.tenant_id, summary, {"gastos": True}, uploaded_file_id=upload_id
    )
    await db_session.commit()
    assert counts1["otros"] == 1

    counts2 = await importer.insert_confirmed_data(
        db_session, sample_tenant.tenant_id, summary, {"gastos": True}, uploaded_file_id=upload_id
    )
    assert counts2["otros"] == 0  # #6: fila ya vista → no re-captura
    records = await _all_unclassified(db_session, sample_tenant.tenant_id)
    assert len(records) == 1  # un solo registro, no dos


@pytest.mark.asyncio
async def test_sale_links_to_same_file_purchased_product(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Review F2 #2: una compra crea un producto nuevo y, más abajo en el MISMO
    archivo, una venta de ese producto DEBE vincularse. La venta usa
    `_resolve_product` (índices transaccionales); sin el fix, el producto creado
    por la compra no estaba en esos índices y la venta quedaba sin vincular."""
    from sqlalchemy import select as _select  # noqa: PLC0415

    from app.persistence.models.transaction import SaleEntry  # noqa: PLC0415

    summary = {
        "file_type": "spreadsheet",
        "inferred_type": "mixed",
        "multi_sheet": True,
        "mapping_contexts": [
            {"context_id": "sheet:Compras", "entity_type": "expense", "source_kind": "sheet",
             "headers": ["producto", "cantidad", "monto", "costo_unitario", "categoria", "fecha"],
             "fields": None, "preview_rows": [], "row_count": 1},
            {"context_id": "sheet:Ventas", "entity_type": "sale", "source_kind": "sheet",
             "headers": ["producto", "monto", "fecha"], "fields": None, "preview_rows": [],
             "row_count": 1},
        ],
        "gastos_detectados": [
            {"__context__": "sheet:Compras", "producto": "Producto Nuevo Z", "cantidad": "5",
             "monto": "2500", "costo_unitario": "500", "categoria": "mercaderia",
             "fecha": "2024-02-01"},
        ],
        "ventas_detectadas": [
            {"__context__": "sheet:Ventas", "producto": "Producto Nuevo Z", "monto": "800",
             "fecha": "2024-02-02"},
        ],
    }
    await importer.insert_confirmed_data(
        db_session,
        sample_tenant.tenant_id,
        summary,
        context_confirmed={"sheet:Compras": True, "sheet:Ventas": True},
    )

    products = await _all_products(db_session, sample_tenant.tenant_id)
    assert len(products) == 1
    sale = (await db_session.execute(_select(SaleEntry))).scalar_one()
    assert sale.product_id == products[0].id  # la venta linkeó al producto comprado
