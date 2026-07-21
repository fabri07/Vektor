"""F1 (hotfix puente) + F1-fix (review): el confirm de import NUNCA debe
explotar (500) cuando el tenant tiene ≥2 productos ACTIVOS cuyo nombre
colisiona en ``lower(trim(name))``. Antes, tanto el camino single-sheet
in-place (``_insert_confirmed_data_impl``) como el multisheet (``_add_product``
dentro de ``_insert_multisheet_data``) usaban ``scalar_one_or_none()`` sobre un
lookup por nombre que podía devolver ≥2 filas → ``sqlalchemy.exc.MultipleResultsFound``.

F1 NO cambiaba la semántica de identidad de producto (eso es Fase 2): solo
hacía que el lookup ambiguo (count>=2) se detectara sin adivinar, sin tocar
ningún producto existente, sin crear nada — pero la fila se DESCARTABA en
silencio (no quedaba en ninguna bandeja).

El fix de review (mismo PR) corrige eso:
- La fila ambigua se persiste en ``unclassified_records`` (bandeja "Otros")
  vía ``_capture_unclassified`` — ``counts["otros"]`` la cuenta. El warning
  específico de "productos_ambiguos" en ``ingestion.py`` se retiró (evita
  doble conteo): el warning genérico de "Otros" ya la cubre. El contador
  ``counts["productos_ambiguos"]`` se conserva como métrica interna.
- El helper ``_find_product_by_name_tolerant`` recupera el fallback
  normalizado tolerante (``_normalize_name``: "Coca-Cola" ↔ "Coca Cola" ↔
  "Coca_Cola") que tenía el camino single-sheet ANTES de F1, ahora contra
  índices pre-cargados UNA vez por corrida (``_load_product_name_lookup_indexes``)
  — nunca un ``select(Product)`` de entidad completa por fila.
- El log ``ingestion.product_name_ambiguous`` incluye ``row_ref``,
  ``candidate_ids`` y ``match_strategy`` para trazabilidad.

Cubre también el caching intra-corrida (``products_by_norm_name``) que evita
crear 2 productos cuando el MISMO archivo trae 2 filas con el mismo nombre
en un solo bloque (<500 filas), reproduciendo el escenario real de prod
(``autoflush=False``) con ``isolated_db_engine``.
"""

from __future__ import annotations

import unittest.mock
import uuid
from decimal import Decimal
from typing import Any

import pytest
import structlog
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

import app.application.services.ingestion_import_service as importer
from app.persistence.models.file import PROCESSING_STATUS_NEEDS_CONFIRMATION, UploadedFile
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


async def _create_products(
    db_session: AsyncSession,
    tenant_id: uuid.UUID,
    names: list[str],
    *,
    is_active: bool = True,
) -> list[Product]:
    """Productos preexistentes con nombres arbitrarios (variantes de formato)."""
    products = [
        Product(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            name=name,
            sale_price_ars=Decimal("1000"),
            unit_cost_ars=Decimal("600"),
            stock_units=5,
            is_active=is_active,
        )
        for name in names
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


# ── FIX 1 (Alto): la fila ambigua queda persistida en "Otros" ─────────────────


@pytest.mark.asyncio
async def test_single_sheet_ambiguous_row_captured_to_otros(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Camino B: la fila ambigua ya NO se descarta en silencio — queda en
    ``unclassified_records`` (PENDING, suggested_entity="product") y
    ``counts["otros"]`` la cuenta."""
    await _create_two_active_products_same_name(db_session, sample_tenant.tenant_id)
    summary = _stock_summary(
        [{"producto": "Coca Cola", "precio": "1500", "costo": "900", "stock": "50"}]
    )

    counts = await importer.insert_confirmed_data(
        db_session, sample_tenant.tenant_id, summary, {"productos": True}
    )

    assert counts["productos"] == 0
    assert counts["otros"] == 1
    assert counts["productos_ambiguos"] == 1  # métrica interna conservada

    records = (
        await db_session.execute(
            select(UnclassifiedRecord).where(
                UnclassifiedRecord.tenant_id == sample_tenant.tenant_id
            )
        )
    ).scalars().all()
    assert len(records) == 1
    assert records[0].status == UNCLASSIFIED_STATUS_PENDING
    assert records[0].suggested_entity == "product"
    assert records[0].source == "ingestion"
    assert "2 productos activos" in (records[0].context_label or "")
    assert records[0].row_data["producto"] == "Coca Cola"


@pytest.mark.asyncio
async def test_multisheet_ambiguous_row_captured_to_otros(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Camino A (multisheet _add_product): mismo comportamiento que single-sheet."""
    await _create_two_active_products_same_name(db_session, sample_tenant.tenant_id)
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

    assert counts["productos"] == 0
    assert counts["otros"] == 1
    assert counts["productos_ambiguos"] == 1

    records = (
        await db_session.execute(
            select(UnclassifiedRecord).where(
                UnclassifiedRecord.tenant_id == sample_tenant.tenant_id
            )
        )
    ).scalars().all()
    assert len(records) == 1
    assert records[0].status == UNCLASSIFIED_STATUS_PENDING
    assert records[0].suggested_entity == "product"
    assert records[0].row_data["producto"] == "Coca Cola"


@pytest.mark.asyncio
async def test_confirm_endpoint_ambiguous_product_warns_via_otros(
    client: AsyncClient,
    auth_headers: dict[str, Any],
    db_session: AsyncSession,
    sample_tenant: Tenant,
    mock_score_trigger: unittest.mock.MagicMock,
) -> None:
    """FIX 5.1: ``ConfirmIngestionResponse.warnings`` refleja la fila ambigua vía
    el warning genérico de "Otros" — el warning específico de "nombre
    duplicado" (``productos_ambiguos``) se retiró para evitar doble conteo.

    Incluye una 2da fila NO ambigua para que el import no quede en 0 filas
    insertadas (``check_nonempty_import`` cortaría con 422 si TODO el archivo
    fuera ambiguo — comportamiento intencional, no es lo que este test cubre).
    """
    await _create_two_active_products_same_name(db_session, sample_tenant.tenant_id)
    record = UploadedFile(
        tenant_id=sample_tenant.tenant_id,
        uploaded_by=None,
        original_filename="productos.xlsx",
        s3_key="uploads/test/uuid/productos.xlsx",
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        size_bytes=1024,
        purpose="stock",
        status="uploaded",
        processing_status=PROCESSING_STATUS_NEEDS_CONFIRMATION,
        parsed_summary_json=_stock_summary(
            [
                {"producto": "Coca Cola", "precio": "1500", "costo": "900", "stock": "50"},
                {"producto": "Alfajor Nuevo", "precio": "800", "costo": "400", "stock": "10"},
            ]
        ),
    )
    db_session.add(record)
    await db_session.commit()

    response = await client.post(
        f"/api/v1/ingestion/files/{record.id}/confirm",
        headers=auth_headers,
        json={"confirmed_fields": {"productos": True}},
    )

    assert response.status_code == 200
    data = response.json()
    assert any("Otros" in w for w in data["warnings"])
    assert not any("nombre duplicado" in w for w in data["warnings"])

    records = (
        await db_session.execute(
            select(UnclassifiedRecord).where(
                UnclassifiedRecord.tenant_id == sample_tenant.tenant_id
            )
        )
    ).scalars().all()
    assert len(records) == 1
    assert records[0].suggested_entity == "product"


# ── FIX 2 (Alto): fallback normalizado tolerante (índices pre-cargados) ───────


@pytest.mark.asyncio
async def test_single_sheet_normalized_variant_updates_existing_no_duplicate(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """"Coca-Cola" existente + fila "Coca Cola" → matchea por fallback
    normalizado (``_normalize_name`` colapsa "-" a espacio), actualiza el
    existente, NO crea un duplicado."""
    existing = (
        await _create_products(db_session, sample_tenant.tenant_id, ["Coca-Cola"])
    )[0]
    summary = _stock_summary(
        [{"producto": "Coca Cola", "precio": "1500", "costo": "900", "stock": "50"}]
    )

    counts = await importer.insert_confirmed_data(
        db_session, sample_tenant.tenant_id, summary, {"productos": True}
    )

    assert counts["productos"] == 1
    assert counts["productos_ambiguos"] == 0
    assert counts["otros"] == 0

    products = (
        await db_session.execute(
            select(Product).where(Product.tenant_id == sample_tenant.tenant_id)
        )
    ).scalars().all()
    assert len(products) == 1
    assert products[0].id == existing.id
    assert products[0].sale_price_ars == Decimal("1500")
    assert products[0].stock_units == 50


@pytest.mark.asyncio
async def test_multisheet_normalized_variant_updates_existing_no_duplicate(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Mismo escenario que arriba, camino multisheet ``_add_product``."""
    existing = (
        await _create_products(db_session, sample_tenant.tenant_id, ["Coca-Cola"])
    )[0]
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

    assert counts["productos"] == 1
    assert counts["productos_ambiguos"] == 0
    assert counts["otros"] == 0

    products = (
        await db_session.execute(
            select(Product).where(Product.tenant_id == sample_tenant.tenant_id)
        )
    ).scalars().all()
    assert len(products) == 1
    assert products[0].id == existing.id
    assert products[0].sale_price_ars == Decimal("1500")


@pytest.mark.asyncio
async def test_two_normalized_variants_same_norm_is_ambiguous(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Dos productos existentes con distinto nombre EXACTO pero mismo
    ``_normalize_name`` ("Coca-Cola" / "Coca_Cola" → ambos "coca cola") + una
    fila cuyo nombre exacto ("Coca Cola") no matchea NINGUNO de los dos
    literalmente (fuerza el fallback normalizado, no el match exacto) →
    ambiguo, va a Otros, no toca ni crea nada."""
    products = await _create_products(
        db_session, sample_tenant.tenant_id, ["Coca-Cola", "Coca_Cola"]
    )
    summary = _stock_summary(
        [{"producto": "Coca Cola", "precio": "1500", "costo": "900", "stock": "50"}]
    )

    counts = await importer.insert_confirmed_data(
        db_session, sample_tenant.tenant_id, summary, {"productos": True}
    )

    assert counts["productos"] == 0
    assert counts["productos_ambiguos"] == 1
    assert counts["otros"] == 1

    refreshed = (
        await db_session.execute(
            select(Product).where(Product.tenant_id == sample_tenant.tenant_id)
        )
    ).scalars().all()
    assert len(refreshed) == 2
    by_id = {p.id: p for p in refreshed}
    for original in products:
        assert by_id[original.id].sale_price_ars == original.sale_price_ars
        assert by_id[original.id].stock_units == original.stock_units


# ── FIX 5.4/5.5: aislamiento entre tenants + productos inactivos ──────────────


@pytest.mark.asyncio
async def test_same_name_in_other_tenant_is_not_a_candidate(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Un producto con el mismo nombre en OTRO tenant no debe contar como
    candidato ni generar ambigüedad falsa."""
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
    # 2 productos "Coca Cola" en el OTRO tenant (colisión ajena, no debe pesar).
    await _create_two_active_products_same_name(db_session, other_tenant.tenant_id)
    # 1 producto "Coca Cola" en el tenant bajo test (no ambiguo acá).
    existing = (
        await _create_products(db_session, sample_tenant.tenant_id, ["Coca Cola"])
    )[0]

    summary = _stock_summary(
        [{"producto": "Coca Cola", "precio": "1500", "costo": "900", "stock": "50"}]
    )
    counts = await importer.insert_confirmed_data(
        db_session, sample_tenant.tenant_id, summary, {"productos": True}
    )

    assert counts["productos"] == 1
    assert counts["productos_ambiguos"] == 0
    assert counts["otros"] == 0

    products = (
        await db_session.execute(
            select(Product).where(Product.tenant_id == sample_tenant.tenant_id)
        )
    ).scalars().all()
    assert len(products) == 1
    assert products[0].id == existing.id
    assert products[0].sale_price_ars == Decimal("1500")


@pytest.mark.asyncio
async def test_inactive_product_does_not_cause_false_ambiguity(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Un producto INACTIVO con el mismo nombre no debe contar como candidato:
    con 1 activo + 1 inactivo, el lookup sigue siendo count==1 (no ambiguo)."""
    active = (
        await _create_products(
            db_session, sample_tenant.tenant_id, ["Coca Cola"], is_active=True
        )
    )[0]
    await _create_products(
        db_session, sample_tenant.tenant_id, ["Coca Cola"], is_active=False
    )

    summary = _stock_summary(
        [{"producto": "Coca Cola", "precio": "1500", "costo": "900", "stock": "50"}]
    )
    counts = await importer.insert_confirmed_data(
        db_session, sample_tenant.tenant_id, summary, {"productos": True}
    )

    assert counts["productos"] == 1
    assert counts["productos_ambiguos"] == 0
    assert counts["otros"] == 0

    products = (
        await db_session.execute(
            select(Product)
            .where(Product.tenant_id == sample_tenant.tenant_id)
            .where(Product.is_active.is_(True))
        )
    ).scalars().all()
    assert len(products) == 1
    assert products[0].id == active.id
    assert products[0].sale_price_ars == Decimal("1500")


# ── FIX 3 (Medio) / FIX 5.6: trazabilidad del log estructurado ────────────────


@pytest.mark.asyncio
async def test_ambiguous_log_includes_row_ref_candidate_ids_and_strategy(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """El log ``ingestion.product_name_ambiguous`` incluye ``row_ref``,
    ``candidate_ids`` (como str) y ``match_strategy`` — no solo
    ``tenant_id``/``normalized_name``/``count`` como en F1."""
    products = await _create_two_active_products_same_name(
        db_session, sample_tenant.tenant_id
    )
    expected_ids = {str(p.id) for p in products}
    summary = _stock_summary(
        [{"producto": "Coca Cola", "precio": "1500", "costo": "900", "stock": "50"}]
    )

    with structlog.testing.capture_logs() as captured:
        await importer.insert_confirmed_data(
            db_session, sample_tenant.tenant_id, summary, {"productos": True}
        )

    ambiguous_events = [
        e for e in captured if e.get("event") == "ingestion.product_name_ambiguous"
    ]
    assert len(ambiguous_events) == 1
    event = ambiguous_events[0]
    assert event["count"] == 2
    # F2-T2: el lookup name-only de F1 (``_find_product_by_name_tolerant`` +
    # ``_load_product_name_lookup_indexes``) fue reemplazado por el motor de
    # identidad de ``test_ingestion_product_identity.py`` — ``match_strategy``
    # ahora es el ``status`` de ``ProductResolution`` ("ambiguous"/"conflict"),
    # no la estrategia interna de match de nombre ("lower_trim"/etc.).
    assert event["match_strategy"] == "ambiguous"
    assert set(event["candidate_ids"]) == expected_ids
    assert "row_ref" in event
    assert "uploaded_file_id" in event


@pytest.mark.asyncio
async def test_load_product_identity_indexes_and_resolve_helper_unit(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Unit test directo del motor de identidad (F2-T2, reemplaza al helper
    name-only de F1 eliminado): índices pre-cargados UNA vez,
    ``_resolve_product_identity`` resuelve/crea/ambigua correctamente."""
    unique = (
        await _create_products(db_session, sample_tenant.tenant_id, ["Agua 500ml"])
    )[0]
    hyphen_variant = (
        await _create_products(db_session, sample_tenant.tenant_id, ["Sprite-500ml"])
    )[0]
    ambiguous_pair = await _create_products(
        db_session, sample_tenant.tenant_id, ["Fanta-Naranja", "Fanta_Naranja"]
    )

    indexes = await importer._load_product_identity_indexes(
        db_session, sample_tenant.tenant_id
    )

    # (1) match exacto único → resolved.
    resolution = importer._resolve_product_identity(
        "Agua 500ml", None, None, indexes=indexes
    )
    assert resolution.status == "resolved"
    assert resolution.product_id == unique.id

    # (2) fallback normalizado único (guion → espacio) → resolved.
    resolution = importer._resolve_product_identity(
        "Sprite 500ml", None, None, indexes=indexes
    )
    assert resolution.status == "resolved"
    assert resolution.product_id == hyphen_variant.id

    # (3) ambiguo por fallback normalizado → sin desambiguador, candidatos completos.
    resolution = importer._resolve_product_identity(
        "Fanta Naranja", None, None, indexes=indexes
    )
    assert resolution.status == "ambiguous"
    assert resolution.product_id is None
    assert {c["id"] for c in resolution.candidates} == {
        str(p.id) for p in ambiguous_pair
    }

    # (4) sin ningún match → create.
    resolution = importer._resolve_product_identity(
        "Producto Inexistente", None, None, indexes=indexes
    )
    assert resolution.status == "create"
    assert resolution.product_id is None
    assert resolution.candidates == []
