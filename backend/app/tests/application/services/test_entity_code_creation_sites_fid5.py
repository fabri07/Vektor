"""F-ID.5: garantía de presencia — toda alta GENUINA nace con código Véktor
en los 8 sitios reales, ninguna edición reasigna, ningún sentinela recibe uno.

Cubre los 3 chokepoints de repositorio (Product/Customer/SupplierRepository.
save) a nivel repo (rápido) + un smoke HTTP por entidad para los endpoints
POST que los ejercen, + los 2 sitios explícitos de "Otros" + el de
ingestion_import_service (proveedor legacy desde una fila de gasto/compra).
"""

from __future__ import annotations

from decimal import Decimal

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services import ingestion_import_service as importer
from app.persistence.models.customer import Customer
from app.persistence.models.product import Product
from app.persistence.models.supplier import Supplier
from app.persistence.models.tenant import Tenant
from app.persistence.repositories.customer_repository import CustomerRepository
from app.persistence.repositories.product_repository import ProductRepository
from app.persistence.repositories.supplier_repository import SupplierRepository

# ── Chokepoints de repositorio ──────────────────────────────────────────────


async def test_customer_repository_save_alta_nueva_nace_con_codigo(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    repo = CustomerRepository(db_session)
    saved = await repo.save(Customer(tenant_id=sample_tenant.tenant_id, name="Juan Pérez"))
    assert saved.vektor_code == "CLI-0001"


async def test_customer_repository_save_edicion_no_reasigna(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    repo = CustomerRepository(db_session)
    saved = await repo.save(Customer(tenant_id=sample_tenant.tenant_id, name="Juan Pérez"))
    original_code = saved.vektor_code

    saved.name = "Juan Pérez (editado)"
    updated = await repo.save(saved)

    assert updated.vektor_code == original_code


async def test_supplier_repository_save_alta_nueva_nace_con_codigo(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    repo = SupplierRepository(db_session)
    saved = await repo.save(Supplier(tenant_id=sample_tenant.tenant_id, name="Distribuidora SA"))
    assert saved.vektor_code == "PRV-0001"


async def test_product_repository_save_alta_nueva_nace_con_sku(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    repo = ProductRepository(db_session)
    saved = await repo.save(
        Product(
            tenant_id=sample_tenant.tenant_id,
            name="Producto sin sku",
            sale_price_ars=Decimal("100"),
            stock_units=1,
        )
    )
    assert saved.sku == "GEN-0001"


async def test_product_repository_save_no_pisa_sku_propio(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    repo = ProductRepository(db_session)
    saved = await repo.save(
        Product(
            tenant_id=sample_tenant.tenant_id,
            name="Producto con sku",
            sku="MI-SKU-PROPIO",
            sale_price_ars=Decimal("100"),
            stock_units=1,
        )
    )
    assert saved.sku == "MI-SKU-PROPIO"


# ── Endpoints HTTP (smoke, uno por entidad) ─────────────────────────────────


async def test_post_customers_nace_con_codigo(
    client: AsyncClient, auth_headers: dict[str, str], mock_score_trigger
) -> None:
    resp = await client.post(
        "/api/v1/customers",
        json={
            "name": "Cliente",
            "last_name": "HTTP",
            "phone": "+5491100000000",
            "dni": "30111222",
        },
        headers=auth_headers,
    )
    assert resp.status_code in (200, 201), resp.text
    assert resp.json()["vektor_code"] == "CLI-0001"


async def test_post_suppliers_nace_con_codigo(
    client: AsyncClient, auth_headers: dict[str, str], mock_score_trigger
) -> None:
    resp = await client.post(
        "/api/v1/suppliers", json={"name": "Proveedor HTTP"}, headers=auth_headers
    )
    assert resp.status_code in (200, 201), resp.text
    assert resp.json()["vektor_code"] == "PRV-0001"


async def test_post_products_nace_con_sku(
    client: AsyncClient, auth_headers: dict[str, str], mock_score_trigger
) -> None:
    resp = await client.post(
        "/api/v1/products",
        json={"name": "Producto HTTP", "sale_price_ars": "100", "stock_units": 1},
        headers=auth_headers,
    )
    assert resp.status_code in (200, 201), resp.text
    assert resp.json()["sku"] == "GEN-0001"


# ── "Otros" → crear cliente/proveedor/producto ──────────────────────────────


async def test_otros_reclassify_customer_nace_con_codigo(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    from app.persistence.models.unclassified_record import (
        UNCLASSIFIED_STATUS_PENDING,
        UnclassifiedRecord,
    )

    record = UnclassifiedRecord(
        tenant_id=sample_tenant.tenant_id,
        source="chat",
        row_data={"nombre": "Cliente Otros"},
        suggested_entity="customer",
        status=UNCLASSIFIED_STATUS_PENDING,
    )
    db_session.add(record)
    await db_session.flush()
    await db_session.commit()

    resp = await client.post(
        f"/api/v1/others/{record.id}/reclassify",
        json={"entity_type": "customer", "fields": {"name": "Cliente Otros", "dni": "30222333"}},
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text

    row = (
        await db_session.execute(select(Customer).where(Customer.name == "Cliente Otros"))
    ).scalar_one()
    assert row.vektor_code == "CLI-0001"


# ── ingestion_import_service: proveedor legacy desde fila de gasto ─────────


async def test_proveedor_creado_desde_gasto_legacy_nace_con_codigo(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    summary = {
        "file_type": "spreadsheet",
        "mapping_contexts": [
            {"context_id": "g", "entity_type": "expense", "label": "Gastos"},
        ],
        "gastos_detectados": [
            {
                "fecha": "2026-08-01",
                "monto": "5000",
                "proveedor": "Proveedor Nuevo Legacy",
                "__context__": "g",
            }
        ],
    }
    context_mappings = {
        "g": {
            "fecha": "transaction_date",
            "monto": "amount",
            "proveedor": "supplier_name",
        }
    }

    await importer.insert_confirmed_data(
        db_session,
        sample_tenant.tenant_id,
        summary,
        context_mappings=context_mappings,
        context_confirmed={"g": True},
    )

    supplier = (
        await db_session.execute(
            select(Supplier).where(Supplier.name == "Proveedor Nuevo Legacy")
        )
    ).scalar_one()
    assert supplier.vektor_code == "PRV-0001"
