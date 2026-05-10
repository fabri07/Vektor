"""Integration tests para DataRepairService — SQLite in-memory, sin infraestructura."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.models.file import UploadedFile
from app.persistence.models.product import Product
from app.persistence.models.repair import DataRepairItem, DataRepairRun
from app.persistence.models.transaction import SaleEntry
from app.tests.integration.conftest import make_tenant, make_user


@pytest_asyncio.fixture
async def tenant(session: AsyncSession):
    return await make_tenant(session, legal_name="Kiosco Repair Test")


@pytest_asyncio.fixture
async def user(session: AsyncSession, tenant):
    return await make_user(session, tenant.tenant_id)


async def _make_uploaded_file(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    parsed_summary_json: dict,
    created_at: datetime | None = None,
) -> UploadedFile:
    f = UploadedFile(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        uploaded_by=user_id,
        original_filename="productos.csv",
        s3_key="test/productos.csv",
        content_type="text/csv",
        size_bytes=1024,
        purpose="chat",
        status="uploaded",
        processing_status="DONE",
        parsed_summary_json=parsed_summary_json,
    )
    if created_at:
        f.created_at = created_at
        f.updated_at = created_at
    session.add(f)
    await session.flush()
    return f


async def _make_import_sale(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    amount: Decimal,
    created_at: datetime | None = None,
) -> SaleEntry:
    s = SaleEntry(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        amount=amount,
        quantity=1,
        transaction_date=datetime.now(UTC).date(),
        payment_method="cash",
        notes="Importado desde archivo",
        provenance="REAL",
    )
    if created_at:
        s.created_at = created_at
        s.updated_at = created_at
    session.add(s)
    await session.flush()
    return s


# ── Test: dry-run persiste run pero no modifica SaleEntry ─────────────────────

@pytest.mark.asyncio
async def test_dry_run_persists_run_without_voiding(session: AsyncSession, tenant, user):
    now = datetime.now(UTC)
    product_summary = {
        "has_fecha": True,
        "has_venta": False,
        "has_gasto": False,
        "has_producto": True,
        "columns": ["fecha", "nombre", "precio"],
        "inferred_type": "ventas",  # original (incorrecto)
        "stock_detectado": [{"nombre": "Coca-Cola 600ml", "precio": "500"}],
        "ventas_detectadas": [{"nombre": "X", "monto": "500"}],
        "preview_rows": [{"nombre": "Coca-Cola 600ml", "precio": "500"}],
        "file_type": "spreadsheet",
    }
    uploaded_file = await _make_uploaded_file(
        session, tenant.tenant_id, user.user_id, product_summary, created_at=now
    )
    sale = await _make_import_sale(
        session, tenant.tenant_id, Decimal("500"), created_at=now
    )
    await session.flush()

    from app.application.services.data_repair_service import apply_repair

    result = await apply_repair(session, tenant_id=tenant.tenant_id, dry_run=True)

    # Run creado y persistido
    assert result.dry_run is True
    assert result.candidates_found >= 1
    assert result.sales_voided == 0  # dry-run: no voidea

    run_in_db = await session.get(DataRepairRun, result.run_id)
    assert run_in_db is not None
    assert run_in_db.status == "COMPLETED"
    assert run_in_db.dry_run is True

    # SaleEntry no modificada
    await session.refresh(sale)
    assert sale.voided_at is None

    # DataRepairItems planificados existen
    items_result = await session.execute(
        select(DataRepairItem).where(DataRepairItem.run_id == result.run_id)
    )
    items = list(items_result.scalars().all())
    assert len(items) > 0
    void_items = [i for i in items if i.action == "VOID_SALE"]
    assert len(void_items) >= 1


# ── Test: apply anula ventas y crea productos ─────────────────────────────────

@pytest.mark.asyncio
async def test_apply_voids_sales_and_creates_products(session: AsyncSession, tenant, user):
    now = datetime.now(UTC)
    product_summary = {
        "has_fecha": False,
        "has_venta": False,
        "has_gasto": False,
        "has_producto": True,
        "columns": ["nombre", "precio"],
        "inferred_type": "ventas",
        "stock_detectado": [{"nombre": "Agua Mineral 1.5L", "precio": "300"}],
        "ventas_detectadas": [{"monto": "300"}],
        "preview_rows": [{"nombre": "Agua Mineral 1.5L", "precio": "300"}],
        "file_type": "spreadsheet",
    }
    uploaded_file = await _make_uploaded_file(
        session, tenant.tenant_id, user.user_id, product_summary, created_at=now
    )
    sale = await _make_import_sale(
        session, tenant.tenant_id, Decimal("300"), created_at=now
    )
    await session.flush()

    from app.application.services.data_repair_service import apply_repair

    result = await apply_repair(session, tenant_id=tenant.tenant_id, dry_run=False)

    assert result.sales_voided >= 1
    assert result.products_created >= 1

    # SaleEntry anulada
    await session.refresh(sale)
    assert sale.voided_at is not None
    assert sale.void_reason == "REPAIR_MISCLASSIFIED_IMPORT"
    assert sale.voided_by_repair_run_id == result.run_id

    # Producto creado
    prod_result = await session.execute(
        select(Product).where(
            Product.tenant_id == tenant.tenant_id,
            Product.name == "Agua Mineral 1.5L",
        )
    )
    product = prod_result.scalar_one_or_none()
    assert product is not None
    assert product.sale_price_ars == Decimal("300")


# ── Test: list_by_tenant no devuelve ventas anuladas ─────────────────────────

@pytest.mark.asyncio
async def test_voided_sales_excluded_from_list(session: AsyncSession, tenant, user):
    """SaleEntry con voided_at seteado no debe aparecer en SaleRepository.list_by_tenant."""
    now = datetime.now(UTC)
    product_summary = {
        "has_fecha": False,
        "has_venta": False,
        "has_gasto": False,
        "has_producto": True,
        "columns": ["nombre", "precio"],
        "inferred_type": "ventas",
        "stock_detectado": [{"nombre": "Yerba Mate 1kg", "precio": "1200"}],
        "ventas_detectadas": [{"monto": "1200"}],
        "preview_rows": [{"nombre": "Yerba Mate 1kg", "precio": "1200"}],
        "file_type": "spreadsheet",
    }
    await _make_uploaded_file(
        session, tenant.tenant_id, user.user_id, product_summary, created_at=now
    )
    # Crear una venta legítima además de la incorrecta
    legit_sale = SaleEntry(
        id=uuid.uuid4(),
        tenant_id=tenant.tenant_id,
        amount=Decimal("5000"),
        quantity=1,
        transaction_date=now.date(),
        payment_method="transfer",
        notes="Venta manual",
        provenance="REAL",
    )
    session.add(legit_sale)
    await _make_import_sale(session, tenant.tenant_id, Decimal("1200"), created_at=now)
    await session.flush()

    from app.application.services.data_repair_service import apply_repair
    from app.persistence.repositories.transaction_repository import SaleRepository

    await apply_repair(session, tenant_id=tenant.tenant_id, dry_run=False)

    repo = SaleRepository(session)
    sales = await repo.list_by_tenant(tenant.tenant_id)

    amounts_decimal = {Decimal(str(s.amount)) for s in sales}
    # La venta legítima sigue
    assert Decimal("5000") in amounts_decimal
    # La venta importada incorrecta NO aparece
    assert Decimal("1200") not in amounts_decimal


# ── Test: source_run_id aplica exactamente el plan del dry-run ───────────────

@pytest.mark.asyncio
async def test_apply_with_source_run_id_marks_dry_run_applied(session: AsyncSession, tenant, user):
    """apply con source_run_id → dry-run previo marcado APPLIED, solo voidea las ventas planificadas."""
    now = datetime.now(UTC)
    product_summary = {
        "has_fecha": False,
        "has_venta": False,
        "has_gasto": False,
        "has_producto": True,
        "columns": ["nombre", "precio"],
        "inferred_type": "ventas",
        "stock_detectado": [{"nombre": "Fanta 600ml", "precio": "600"}],
        "ventas_detectadas": [{"monto": "600"}],
        "preview_rows": [{"nombre": "Fanta 600ml", "precio": "600"}],
        "file_type": "spreadsheet",
    }
    await _make_uploaded_file(
        session, tenant.tenant_id, user.user_id, product_summary, created_at=now
    )
    sale = await _make_import_sale(session, tenant.tenant_id, Decimal("600"), created_at=now)
    await session.flush()

    from app.application.services.data_repair_service import apply_repair
    from app.persistence.models.repair import DataRepairRun

    # 1. Dry-run
    dry_result = await apply_repair(session, tenant_id=tenant.tenant_id, dry_run=True)
    assert dry_result.dry_run is True
    dry_run_obj = await session.get(DataRepairRun, dry_result.run_id)
    assert dry_run_obj is not None
    assert dry_run_obj.status == "COMPLETED"

    # 2. Apply desde el dry-run
    apply_result = await apply_repair(
        session,
        tenant_id=tenant.tenant_id,
        dry_run=False,
        source_run_id=dry_result.run_id,
    )
    assert apply_result.sales_voided == 1

    # El dry-run queda marcado como APPLIED
    await session.refresh(dry_run_obj)
    assert dry_run_obj.status == "APPLIED"

    # La venta fue anulada
    await session.refresh(sale)
    assert sale.voided_at is not None


# ── Test: normalización bidireccional evita duplicados ───────────────────────

@pytest.mark.asyncio
async def test_normalization_prevents_duplicate_with_hyphen_variant(session: AsyncSession, tenant, user):
    """'Coca Cola' importado cuando existe 'Coca-Cola' → UPDATE, no CREATE duplicado."""
    now = datetime.now(UTC)

    # Producto existente con guión
    existing = Product(
        id=uuid.uuid4(),
        tenant_id=tenant.tenant_id,
        name="Coca-Cola 600ml",
        sale_price_ars=Decimal("500"),
        stock_units=10,
        provenance="REAL",
    )
    session.add(existing)
    await session.flush()

    # CSV importa con espacio (sin guión)
    product_summary = {
        "has_fecha": False,
        "has_venta": False,
        "has_gasto": False,
        "has_producto": True,
        "columns": ["nombre", "precio"],
        "inferred_type": "ventas",
        "stock_detectado": [{"nombre": "Coca Cola 600ml", "precio": "550"}],
        "ventas_detectadas": [{"monto": "550"}],
        "preview_rows": [{"nombre": "Coca Cola 600ml", "precio": "550"}],
        "file_type": "spreadsheet",
    }
    await _make_uploaded_file(
        session, tenant.tenant_id, user.user_id, product_summary, created_at=now
    )
    await _make_import_sale(session, tenant.tenant_id, Decimal("550"), created_at=now)
    await session.flush()

    from app.application.services.data_repair_service import apply_repair

    result = await apply_repair(session, tenant_id=tenant.tenant_id, dry_run=False)

    # El apply debe UPDATE, no duplicar
    prod_result = await session.execute(
        select(Product).where(
            Product.tenant_id == tenant.tenant_id,
        )
    )
    products = list(prod_result.scalars().all())
    # Solo 1 producto (no hubo duplicado)
    assert len(products) == 1
    # El precio fue actualizado al nuevo
    assert products[0].sale_price_ars == Decimal("550")
    assert result.products_updated == 1
    assert result.products_created == 0


# ── Test: run idempotente ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_apply_twice_is_idempotent(session: AsyncSession, tenant, user):
    """Correr apply dos veces no duplica productos ni anula dos veces."""
    now = datetime.now(UTC)
    product_summary = {
        "has_fecha": False,
        "has_venta": False,
        "has_gasto": False,
        "has_producto": True,
        "columns": ["nombre", "precio"],
        "inferred_type": "ventas",
        "stock_detectado": [{"nombre": "Azucar 1kg", "precio": "700"}],
        "ventas_detectadas": [{"monto": "700"}],
        "preview_rows": [{"nombre": "Azucar 1kg", "precio": "700"}],
        "file_type": "spreadsheet",
    }
    await _make_uploaded_file(
        session, tenant.tenant_id, user.user_id, product_summary, created_at=now
    )
    await _make_import_sale(session, tenant.tenant_id, Decimal("700"), created_at=now)
    await session.flush()

    from app.application.services.data_repair_service import apply_repair

    # Primera vez
    r1 = await apply_repair(session, tenant_id=tenant.tenant_id, dry_run=False)
    assert r1.sales_voided == 1

    # Segunda vez: la venta ya está anulada (voided_at IS NULL filtra), así que 0 candidatos
    r2 = await apply_repair(session, tenant_id=tenant.tenant_id, dry_run=False)
    assert r2.sales_voided == 0

    # Producto no se duplica
    prod_result = await session.execute(
        select(Product).where(
            Product.tenant_id == tenant.tenant_id,
            Product.name == "Azucar 1kg",
        )
    )
    products = list(prod_result.scalars().all())
    assert len(products) == 1
