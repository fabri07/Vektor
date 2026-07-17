"""Tests del listener ``before_insert``/``before_update`` de ``Product`` (Fase 2, T1).

El listener es la fuente ÚNICA de cálculo de las columnas ``*_normalized``:
cubre los 6 ``session.add(Product(...))`` del código sin depender de timing de
flush explícito por parte del caller (dispara en flush, no en construcción).
"""

from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant


async def test_listener_fills_normalized_columns_on_insert(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    product = Product(
        tenant_id=sample_tenant.tenant_id,
        name="Coca-Cola",
        sku="CC 500",
        barcode="7790895000123",
        custom_fields={"marca": "Café Martínez"},
        sale_price_ars=Decimal("100"),
    )
    db_session.add(product)
    await db_session.flush()

    assert product.name_normalized == "coca cola"
    assert product.sku_normalized == "cc 500"
    assert product.barcode_normalized == "7790895000123"
    assert product.brand_normalized == "cafe martinez"


async def test_listener_recomputes_on_update(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    product = Product(
        tenant_id=sample_tenant.tenant_id,
        name="Coca-Cola",
        sku="CC 500",
        barcode="7790895000123",
        custom_fields={"marca": "Café Martínez"},
        sale_price_ars=Decimal("100"),
    )
    db_session.add(product)
    await db_session.commit()

    product.name = "Sprite Zero"
    product.barcode = "779-000 111"
    await db_session.commit()

    assert product.name_normalized == "sprite zero"
    assert product.barcode_normalized == "779000111"


async def test_listener_handles_missing_custom_fields_marca(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    product = Product(
        tenant_id=sample_tenant.tenant_id,
        name="Producto sin marca",
        sale_price_ars=Decimal("50"),
    )
    db_session.add(product)
    await db_session.flush()

    assert product.brand_normalized is None


async def test_listener_handles_none_barcode(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    product = Product(
        tenant_id=sample_tenant.tenant_id,
        name="Producto sin barcode",
        sale_price_ars=Decimal("50"),
        barcode=None,
    )
    db_session.add(product)
    await db_session.flush()

    assert product.barcode_normalized is None
