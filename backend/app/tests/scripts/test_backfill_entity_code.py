"""Tests de scripts/backfill_entity_code.py (F-ID.6).

Se carga el módulo por ruta de archivo (``scripts/`` no es un paquete) —
mismo patrón que ``test_detect_misvoided_purchases.py``. Ejercita las
funciones ``_procesar_*`` directamente contra SQLite (``db_session``), sin
pasar por el CLI/engine real — eso ya se prueba en los otros scripts.

``insert_decision_audit`` real usa ``gen_random_uuid()``/``now()`` de
Postgres vía SQL crudo, que SQLite no soporta — mismo problema y misma
solución que ``test_reanalyze_ingestion.py``: fixture autouse que lo
reemplaza por un fake equivalente vía ``monkeypatch`` en todo este archivo.
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
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.customer_sentinel import resolve_or_create_local_sentinel
from app.persistence.models.audit import DecisionAuditLog
from app.persistence.models.customer import Customer
from app.persistence.models.product import Product
from app.persistence.models.supplier import Supplier
from app.persistence.models.tenant import Tenant

_SCRIPTS_DIR = Path(__file__).resolve().parents[3] / "scripts"


def _load_module():
    if str(_SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(_SCRIPTS_DIR))
    spec = importlib.util.spec_from_file_location(
        "backfill_entity_code", _SCRIPTS_DIR / "backfill_entity_code.py"
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


async def test_productos_sin_sku_se_numeran(
    mod, db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    # `_vertical_de` resuelve por SQL crudo con `CAST(:tid AS uuid)`, que en
    # SQLite no convierte nada (sin tipo uuid nativo) y nunca matchea — por
    # diseño, igual que `backfill_product_category.py`, sólo se prueba en
    # Postgres real. Acá se confirma el fallback honesto a GEN (nunca crashea
    # por no resolver vertical), no el prefijo por categoría.
    p1 = Product(
        tenant_id=sample_tenant.tenant_id,
        name="Producto 1",
        sale_price_ars=Decimal("100"),
        stock_units=1,
        category="BEBIDAS",
    )
    p2 = Product(
        tenant_id=sample_tenant.tenant_id,
        name="Producto con sku",
        sku="YA-TENGO",
        sale_price_ars=Decimal("100"),
        stock_units=1,
    )
    db_session.add_all([p1, p2])
    await db_session.flush()

    conteo, detalle = await mod._procesar_productos(
        db_session, str(sample_tenant.tenant_id), apply=True
    )

    assert conteo[mod._ASIGNADO] == 1
    assert conteo[mod._YA_TENIA] == 1
    assert p1.sku == "GEN-0001"
    assert p2.sku == "YA-TENGO"


async def test_productos_backfill_es_idempotente(
    mod, db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    p1 = Product(
        tenant_id=sample_tenant.tenant_id,
        name="Producto idempotente",
        sale_price_ars=Decimal("100"),
        stock_units=1,
    )
    db_session.add(p1)
    await db_session.flush()

    await mod._procesar_productos(db_session, str(sample_tenant.tenant_id), apply=True)
    first_sku = p1.sku
    conteo2, _ = await mod._procesar_productos(
        db_session, str(sample_tenant.tenant_id), apply=True
    )

    assert p1.sku == first_sku
    assert conteo2[mod._YA_TENIA] >= 1


async def test_clientes_sin_codigo_se_numeran_y_sentinela_se_saltea(
    mod, db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    await resolve_or_create_local_sentinel(db_session, sample_tenant.tenant_id)
    c1 = Customer(tenant_id=sample_tenant.tenant_id, name="Cliente A")
    db_session.add(c1)
    await db_session.flush()

    conteo, detalle = await mod._procesar_simple(
        db_session,
        str(sample_tenant.tenant_id),
        apply=True,
        model=Customer,
        spec=mod.CUSTOMER_CODE_SPEC,
        entity_type="customer",
    )

    assert conteo[mod._ASIGNADO] == 1
    assert c1.vektor_code == "CLI-0001"
    # El sentinela no aparece en el detalle en absoluto.
    assert all(row["name"] != "Local" for row in detalle)


async def test_proveedores_duplicados_reciben_codigo_y_quedan_marcados(
    mod, db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    s1 = Supplier(tenant_id=sample_tenant.tenant_id, name="Distribuidora Norte")
    # mismo normalizado que s1
    s2 = Supplier(tenant_id=sample_tenant.tenant_id, name="distribuidora norte")
    s3 = Supplier(tenant_id=sample_tenant.tenant_id, name="Proveedor Único")
    db_session.add_all([s1, s2, s3])
    await db_session.flush()

    conteo, detalle = await mod._procesar_proveedores(
        db_session, str(sample_tenant.tenant_id), apply=True
    )

    # Los 3 reciben código, aunque 2 queden marcados como posible duplicado.
    assert s1.vektor_code is not None
    assert s2.vektor_code is not None
    assert s3.vektor_code is not None
    assert s1.vektor_code != s2.vektor_code != s3.vektor_code
    assert conteo[mod._POSIBLE_DUPLICADO] == 2
    assert conteo[mod._ASIGNADO] == 1


async def test_proveedores_sin_duplicado_no_quedan_marcados(
    mod, db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    s1 = Supplier(tenant_id=sample_tenant.tenant_id, name="Proveedor Solo A")
    s2 = Supplier(tenant_id=sample_tenant.tenant_id, name="Proveedor Solo B")
    db_session.add_all([s1, s2])
    await db_session.flush()

    conteo, _ = await mod._procesar_proveedores(
        db_session, str(sample_tenant.tenant_id), apply=True
    )

    assert conteo[mod._POSIBLE_DUPLICADO] == 0
    assert conteo[mod._ASIGNADO] == 2
