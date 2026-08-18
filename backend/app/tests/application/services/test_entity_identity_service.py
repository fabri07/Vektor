"""F-ID.2: `assign_vektor_code_if_missing` + `record_identifier`.

Cubre las dos garantías centrales: nunca pisa un código existente (del
negocio o ya generado), nunca asigna al sentinela, y el registro de
identificadores externos es idempotente por valor normalizado dentro de un
namespace — con conflicto explícito si el mismo valor ya pertenece a OTRA
entidad.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.customer_sentinel import resolve_or_create_local_sentinel
from app.application.services.entity_code_service import (
    CUSTOMER_CODE_SPEC,
    PRODUCT_CODE_SPEC,
    SUPPLIER_CODE_SPEC,
    EntityIdentifierConflictError,
    assign_vektor_code_if_missing,
    record_identifier,
)
from app.domain.verticals import Vertical
from app.persistence.models.audit import DecisionAuditLog
from app.persistence.models.customer import Customer
from app.persistence.models.entity_identifier import EntityIdentifier
from app.persistence.models.product import Product
from app.persistence.models.supplier import Supplier
from app.persistence.models.tenant import Tenant


async def test_producto_sin_sku_recibe_codigo_por_categoria(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    product = Product(
        tenant_id=sample_tenant.tenant_id,
        name="Sabana king",
        sale_price_ars=Decimal("15000"),
        stock_units=5,
        category="TEXTILES",
    )
    db_session.add(product)
    await db_session.flush()

    assigned = await assign_vektor_code_if_missing(
        db_session,
        product,
        PRODUCT_CODE_SPEC,
        sample_tenant.tenant_id,
        vertical=Vertical.DECORACION_HOGAR,
        category="TEXTILES",
    )

    assert assigned is True
    assert product.sku == "TEX-0001"
    assert product.custom_fields.get("_sku_origin") == "vektor"

    row = (
        await db_session.execute(
            select(EntityIdentifier).where(
                EntityIdentifier.entity_id == product.id,
                EntityIdentifier.identifier_type == "vektor_code",
            )
        )
    ).scalar_one()
    assert row.raw_value == "TEX-0001"
    assert row.namespace == "vektor"
    assert row.origin == "vektor"
    assert row.is_primary is True


async def test_asignacion_en_vivo_queda_auditada(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Regresión: antes, ``assign_vektor_code_if_missing`` — el path de
    asignación en vivo, usado en TODA alta real de producto/cliente/proveedor —
    nunca escribía en ``decision_audit_log``, a diferencia del backfill offline
    (``ENTITY_CODE_BACKFILL``), violando el invariante "toda decisión → audit"."""
    product = Product(
        tenant_id=sample_tenant.tenant_id,
        name="Sabana king",
        sale_price_ars=Decimal("15000"),
        stock_units=5,
        category="TEXTILES",
    )
    db_session.add(product)
    await db_session.flush()

    await assign_vektor_code_if_missing(
        db_session,
        product,
        PRODUCT_CODE_SPEC,
        sample_tenant.tenant_id,
        vertical=Vertical.DECORACION_HOGAR,
        category="TEXTILES",
    )

    audit = (
        await db_session.execute(
            select(DecisionAuditLog).where(
                DecisionAuditLog.tenant_id == sample_tenant.tenant_id,
                DecisionAuditLog.decision_type == "ENTITY_CODE_ASSIGNED",
            )
        )
    ).scalar_one()
    assert audit.decision_data["entity_type"] == "product"
    assert audit.decision_data["entity_id"] == str(product.id)
    assert audit.decision_data["codigo"] == "TEX-0001"


async def test_asignacion_ya_tenia_codigo_no_audita_de_nuevo(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    product = Product(
        tenant_id=sample_tenant.tenant_id,
        name="Ya con sku",
        sku="PROPIO-1",
        sale_price_ars=Decimal("100"),
        stock_units=1,
    )
    db_session.add(product)
    await db_session.flush()

    assigned = await assign_vektor_code_if_missing(
        db_session, product, PRODUCT_CODE_SPEC, sample_tenant.tenant_id
    )

    assert assigned is False
    rows = (
        await db_session.execute(
            select(DecisionAuditLog).where(
                DecisionAuditLog.tenant_id == sample_tenant.tenant_id,
                DecisionAuditLog.decision_type == "ENTITY_CODE_ASSIGNED",
            )
        )
    ).scalars().all()
    assert rows == []


async def test_producto_con_sku_propio_no_se_pisa(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    product = Product(
        tenant_id=sample_tenant.tenant_id,
        name="Sabana king",
        sku="CC-500-PROPIO",
        sale_price_ars=Decimal("15000"),
        stock_units=5,
    )
    db_session.add(product)
    await db_session.flush()

    assigned = await assign_vektor_code_if_missing(
        db_session, product, PRODUCT_CODE_SPEC, sample_tenant.tenant_id
    )

    assert assigned is False
    assert product.sku == "CC-500-PROPIO"
    assert "_sku_origin" not in (product.custom_fields or {})


async def test_cliente_sin_codigo_recibe_prefijo_cli(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    customer = Customer(tenant_id=sample_tenant.tenant_id, name="Juan Pérez")
    db_session.add(customer)
    await db_session.flush()

    assigned = await assign_vektor_code_if_missing(
        db_session, customer, CUSTOMER_CODE_SPEC, sample_tenant.tenant_id
    )
    await db_session.flush()  # *_normalized se computa en before_update, en flush

    assert assigned is True
    assert customer.vektor_code == "CLI-0001"
    assert customer.vektor_code_normalized == "cli-0001"


async def test_proveedor_sin_codigo_recibe_prefijo_prv(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    supplier = Supplier(tenant_id=sample_tenant.tenant_id, name="Distribuidora SA")
    db_session.add(supplier)
    await db_session.flush()

    assigned = await assign_vektor_code_if_missing(
        db_session, supplier, SUPPLIER_CODE_SPEC, sample_tenant.tenant_id
    )

    assert assigned is True
    assert supplier.vektor_code == "PRV-0001"


async def test_sentinela_nunca_recibe_codigo(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    sentinel_id = await resolve_or_create_local_sentinel(db_session, sample_tenant.tenant_id)
    sentinel = await db_session.get(Customer, sentinel_id)
    assert sentinel is not None

    assigned = await assign_vektor_code_if_missing(
        db_session, sentinel, CUSTOMER_CODE_SPEC, sample_tenant.tenant_id
    )

    assert assigned is False
    assert sentinel.vektor_code is None


async def test_entidad_sin_flushear_lanza_value_error(sample_tenant: Tenant) -> None:
    customer = Customer(tenant_id=sample_tenant.tenant_id, name="Sin flush")
    # No session.add / flush: entity.id todavía es None.
    with pytest.raises(ValueError, match="flushear"):
        await assign_vektor_code_if_missing(
            None,  # type: ignore[arg-type]
            customer,
            CUSTOMER_CODE_SPEC,
            sample_tenant.tenant_id,
        )


async def test_record_identifier_crea_fila_nueva(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    customer = Customer(tenant_id=sample_tenant.tenant_id, name="Almacén Doña Rosa")
    db_session.add(customer)
    await db_session.flush()

    row = await record_identifier(
        db_session,
        sample_tenant.tenant_id,
        "customer",
        customer.id,
        "business_code",
        "business",
        "CLI-918",
        "import",
    )

    assert row.normalized_value == "cli-918"
    assert row.raw_value == "CLI-918"


async def test_record_identifier_es_idempotente_para_la_misma_entidad(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    customer = Customer(tenant_id=sample_tenant.tenant_id, name="Almacén Doña Rosa")
    db_session.add(customer)
    await db_session.flush()

    first = await record_identifier(
        db_session, sample_tenant.tenant_id, "customer", customer.id,
        "business_code", "business", "CLI-918", "import",
    )
    second = await record_identifier(
        db_session, sample_tenant.tenant_id, "customer", customer.id,
        "business_code", "business", "cli-918", "import",  # variante de casing
    )

    assert first.id == second.id
    count = (
        await db_session.execute(
            select(EntityIdentifier).where(EntityIdentifier.entity_id == customer.id)
        )
    ).scalars().all()
    assert len(count) == 1


async def test_record_identifier_conflicto_entre_entidades_distintas(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    a = Customer(tenant_id=sample_tenant.tenant_id, name="Cliente A")
    b = Customer(tenant_id=sample_tenant.tenant_id, name="Cliente B")
    db_session.add_all([a, b])
    await db_session.flush()

    await record_identifier(
        db_session, sample_tenant.tenant_id, "customer", a.id,
        "business_code", "business", "CLI-01", "import",
    )

    with pytest.raises(EntityIdentifierConflictError):
        await record_identifier(
            db_session, sample_tenant.tenant_id, "customer", b.id,
            "business_code", "business", "CLI-01", "import",
        )


async def test_desactivar_no_borra_la_fila_de_identificador(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    from datetime import UTC, datetime

    customer = Customer(tenant_id=sample_tenant.tenant_id, name="Cliente a desactivar")
    db_session.add(customer)
    await db_session.flush()
    await assign_vektor_code_if_missing(
        db_session, customer, CUSTOMER_CODE_SPEC, sample_tenant.tenant_id
    )
    code = customer.vektor_code
    await db_session.flush()

    customer.deactivated_at = datetime.now(UTC)
    await db_session.flush()

    row = (
        await db_session.execute(
            select(EntityIdentifier).where(
                EntityIdentifier.entity_id == customer.id,
                EntityIdentifier.identifier_type == "vektor_code",
            )
        )
    ).scalar_one()
    assert row.raw_value == code
    assert row.revoked_at is None
    assert row.is_primary is True
