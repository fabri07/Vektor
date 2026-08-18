"""Tests de scripts/bootstrap_entity_identifiers.py (F-ID.4).

Mismo patrón que test_backfill_entity_code.py: módulo cargado por ruta,
`insert_decision_audit` parcheado a un fake SQLite-compatible.
"""

from __future__ import annotations

import importlib.util
import sys
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.customer_sentinel import resolve_or_create_local_sentinel
from app.domain.product_alias import add_alias
from app.persistence.models.audit import DecisionAuditLog
from app.persistence.models.customer import Customer
from app.persistence.models.entity_identifier import EntityIdentifier
from app.persistence.models.product import Product
from app.persistence.models.supplier import Supplier
from app.persistence.models.tenant import Tenant

_SCRIPTS_DIR = Path(__file__).resolve().parents[3] / "scripts"


def _load_module():
    if str(_SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(_SCRIPTS_DIR))
    spec = importlib.util.spec_from_file_location(
        "bootstrap_entity_identifiers", _SCRIPTS_DIR / "bootstrap_entity_identifiers.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def mod():
    return _load_module()


async def _sqlite_insert_decision_audit(
    session: AsyncSession,
    *,
    tenant_id: str,
    decision_type: str,
    decision_data: dict[str, Any],
    triggered_by: str,
) -> str:
    audit_id = uuid.uuid4()
    session.add(
        DecisionAuditLog(
            id=audit_id,
            tenant_id=uuid.UUID(tenant_id),
            decision_type=decision_type,
            decision_data=decision_data,
            triggered_by=triggered_by,
            created_at=datetime.now(UTC),
        )
    )
    await session.flush()
    return str(audit_id)


@pytest.fixture(autouse=True)
def _patch_audit_insert(mod, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mod, "insert_decision_audit", _sqlite_insert_decision_audit)


async def test_producto_copia_sku_business_y_barcode(
    mod, db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    product = Product(
        tenant_id=sample_tenant.tenant_id,
        name="Coca Cola",
        sku="CC-500",
        barcode="7791234567890",
        sale_price_ars=Decimal("1500"),
        stock_units=10,
    )
    db_session.add(product)
    await db_session.flush()

    conteo, detalle = await mod._procesar_productos(
        db_session, str(sample_tenant.tenant_id), apply=True
    )

    assert conteo[mod._COPIADO] == 2  # sku + barcode
    rows = (
        await db_session.execute(
            select(EntityIdentifier).where(EntityIdentifier.entity_id == product.id)
        )
    ).scalars().all()
    by_type = {r.identifier_type: r for r in rows}
    assert by_type["sku"].namespace == "business"
    assert by_type["sku"].raw_value == "CC-500"
    assert by_type["barcode"].namespace == "business"
    assert by_type["barcode"].raw_value == "7791234567890"


async def test_producto_sku_generado_por_vektor_usa_namespace_vektor(
    mod, db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    product = Product(
        tenant_id=sample_tenant.tenant_id,
        name="Producto GEN",
        sku="GEN-0001",
        sale_price_ars=Decimal("100"),
        stock_units=1,
        custom_fields={"_sku_origin": "vektor"},
    )
    db_session.add(product)
    await db_session.flush()

    await mod._procesar_productos(db_session, str(sample_tenant.tenant_id), apply=True)

    row = (
        await db_session.execute(
            select(EntityIdentifier).where(
                EntityIdentifier.entity_id == product.id,
                EntityIdentifier.identifier_type == "sku",
            )
        )
    ).scalar_one()
    assert row.namespace == "vektor"
    assert row.origin == "vektor"


async def test_producto_alias_se_copia_como_user_confirmed(
    mod, db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    product = Product(
        tenant_id=sample_tenant.tenant_id,
        name="Coca Cola 500ml",
        sale_price_ars=Decimal("1500"),
        stock_units=1,
        custom_fields=add_alias(None, "Gaseosa cola cualquiera"),
    )
    db_session.add(product)
    await db_session.flush()

    await mod._procesar_productos(db_session, str(sample_tenant.tenant_id), apply=True)

    row = (
        await db_session.execute(
            select(EntityIdentifier).where(
                EntityIdentifier.entity_id == product.id,
                EntityIdentifier.identifier_type == "alias",
            )
        )
    ).scalar_one()
    assert row.raw_value == "Gaseosa cola cualquiera"
    assert row.origin == "user_confirmed"


async def test_cliente_copia_dni_y_cuit_y_saltea_sentinela(
    mod, db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    await resolve_or_create_local_sentinel(db_session, sample_tenant.tenant_id)
    customer = Customer(
        tenant_id=sample_tenant.tenant_id, name="Juan Pérez", dni="30111222", cuit="20301112223"
    )
    db_session.add(customer)
    await db_session.flush()

    conteo, detalle = await mod._procesar_clientes(
        db_session, str(sample_tenant.tenant_id), apply=True
    )

    assert conteo[mod._COPIADO] == 2
    assert all(row["name"] != "Local" for row in detalle)


async def test_proveedor_copia_cuit_y_cuil(
    mod, db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    supplier = Supplier(
        tenant_id=sample_tenant.tenant_id, name="Distribuidora SA", cuit="30711112223"
    )
    db_session.add(supplier)
    await db_session.flush()

    conteo, _ = await mod._procesar_proveedores(
        db_session, str(sample_tenant.tenant_id), apply=True
    )

    assert conteo[mod._COPIADO] == 1  # sólo cuit, cuil vacío


async def test_es_idempotente(mod, db_session: AsyncSession, sample_tenant: Tenant) -> None:
    customer = Customer(tenant_id=sample_tenant.tenant_id, name="Cliente", dni="30111222")
    db_session.add(customer)
    await db_session.flush()

    await mod._procesar_clientes(db_session, str(sample_tenant.tenant_id), apply=True)
    await mod._procesar_clientes(db_session, str(sample_tenant.tenant_id), apply=True)

    rows = (
        await db_session.execute(
            select(EntityIdentifier).where(EntityIdentifier.entity_id == customer.id)
        )
    ).scalars().all()
    assert len(rows) == 1


async def test_valor_duplicado_entre_entidades_distintas_se_cuenta_como_conflicto(
    mod, db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    c1 = Customer(tenant_id=sample_tenant.tenant_id, name="Cliente A", dni="30111222")
    c2 = Customer(tenant_id=sample_tenant.tenant_id, name="Cliente B", dni="30111222")
    db_session.add_all([c1, c2])
    await db_session.flush()

    conteo, detalle = await mod._procesar_clientes(
        db_session, str(sample_tenant.tenant_id), apply=True
    )

    assert conteo[mod._CONFLICTO] == 1
    assert conteo[mod._COPIADO] == 1
    conflictos = [row for row in detalle if row["estado"].startswith(mod._CONFLICTO)]
    assert len(conflictos) == 1


async def test_dry_run_no_escribe_nada(
    mod, db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    customer = Customer(tenant_id=sample_tenant.tenant_id, name="Cliente", dni="30111222")
    db_session.add(customer)
    await db_session.flush()

    conteo, detalle = await mod._procesar_clientes(
        db_session, str(sample_tenant.tenant_id), apply=False
    )

    assert conteo[mod._COPIADO] == 1
    rows = (
        await db_session.execute(
            select(EntityIdentifier).where(EntityIdentifier.entity_id == customer.id)
        )
    ).scalars().all()
    assert len(rows) == 0
