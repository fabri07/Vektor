"""F-ID.10 — `get_entity_ref()`: helper de display de sólo lectura.

Cubre las 3 entidades (código en columna distinta cada una — sku para
producto, vektor_code para cliente/proveedor), el caso sin código (sentinela)
y el caso de no-invención (entidad inexistente / de otro tenant).
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.application.agents.shared.entity_ref import get_entity_ref
from app.application.services.customer_sentinel import (
    LOCAL_CUSTOMER_NAME,
    resolve_or_create_local_sentinel,
)
from app.persistence.models.customer import Customer
from app.persistence.models.product import Product
from app.persistence.models.supplier import Supplier
from app.persistence.models.tenant import Tenant
from app.persistence.repositories.customer_repository import CustomerRepository
from app.persistence.repositories.product_repository import ProductRepository
from app.persistence.repositories.supplier_repository import SupplierRepository


async def test_producto_muestra_nombre_y_sku(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    repo = ProductRepository(db_session)
    product = await repo.save(
        Product(
            tenant_id=sample_tenant.tenant_id,
            name="Coca Cola 500ml",
            sale_price_ars=Decimal("100.00"),
        )
    )
    await db_session.commit()

    ref = await get_entity_ref(db_session, sample_tenant.tenant_id, "product", product.id)

    assert ref is not None
    assert ref.id == str(product.id)
    assert ref.code == product.sku
    assert ref.display_name == f"Coca Cola 500ml ({product.sku})"


async def test_cliente_muestra_nombre_y_vektor_code(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    repo = CustomerRepository(db_session)
    customer = await repo.save(Customer(tenant_id=sample_tenant.tenant_id, name="Juan Pérez"))
    await db_session.commit()
    assert customer.vektor_code == "CLI-0001"

    ref = await get_entity_ref(db_session, sample_tenant.tenant_id, "customer", customer.id)

    assert ref is not None
    assert ref.code == "CLI-0001"
    assert ref.display_name == "Juan Pérez (CLI-0001)"


async def test_proveedor_muestra_nombre_y_vektor_code(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    repo = SupplierRepository(db_session)
    supplier = await repo.save(Supplier(tenant_id=sample_tenant.tenant_id, name="Distribuidora SA"))
    await db_session.commit()

    ref = await get_entity_ref(db_session, sample_tenant.tenant_id, "supplier", supplier.id)

    assert ref is not None
    assert ref.code == "PRV-0001"
    assert ref.display_name == "Distribuidora SA (PRV-0001)"


async def test_centinela_sin_codigo_no_muestra_parentesis_vacio(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    sentinel_id = await resolve_or_create_local_sentinel(db_session, sample_tenant.tenant_id)
    await db_session.commit()

    ref = await get_entity_ref(db_session, sample_tenant.tenant_id, "customer", sentinel_id)

    assert ref is not None
    assert ref.code is None
    assert ref.display_name == LOCAL_CUSTOMER_NAME
    assert "(" not in ref.display_name


async def test_entidad_inexistente_da_none_nunca_inventa(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    ref = await get_entity_ref(db_session, sample_tenant.tenant_id, "product", uuid.uuid4())
    assert ref is None


async def test_entidad_de_otro_tenant_da_none(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    other_tenant_id = uuid.uuid4()
    repo = CustomerRepository(db_session)
    customer = await repo.save(Customer(tenant_id=sample_tenant.tenant_id, name="Juan Pérez"))
    await db_session.commit()

    ref = await get_entity_ref(db_session, other_tenant_id, "customer", customer.id)

    assert ref is None
