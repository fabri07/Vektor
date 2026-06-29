"""Tests F3a: purchase_orders + AgentSupplier persiste draft + catalog_url.

Cubre:
- test_purchase_order_model_migration: persist + releer + JSONB round-trip.
- test_supplier_catalog_url_persisted: catalog_url persiste y vuelve en SupplierResponse.
- test_purchase_order_cross_tenant: list_by_tenant/list_by_supplier no devuelven PO de otro tenant.
- test_purchase_order_routing: INTENT_TO_ACTION_TYPE y INTENT_TO_AGENT correctos.
- test_agent_supplier_purchase_order_creates_draft: con quiebres → crea PO correcto.
- test_agent_supplier_purchase_order_no_quiebres: sin quiebres → no crea PO, mensaje claro.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.agents.ceo.team_plan_builder import INTENT_TO_ACTION_TYPE, INTENT_TO_AGENT
from app.application.agents.shared.schemas import ActionType, AgentRequest
from app.persistence.models.purchase_order import PurchaseOrder
from app.persistence.models.supplier import Supplier
from app.persistence.models.tenant import Tenant
from app.persistence.repositories.purchase_order_repository import PurchaseOrderRepository

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def supplier_fixture(db_session: AsyncSession, sample_tenant: Tenant) -> Supplier:
    """Crea un proveedor activo para el tenant principal."""
    sup = Supplier(
        tenant_id=sample_tenant.tenant_id,
        name="Distribuidora Norte",
        email="norte@ejemplo.com",
    )
    db_session.add(sup)
    await db_session.commit()
    return sup


@pytest_asyncio.fixture
async def product_low_stock(db_session: AsyncSession, sample_tenant: Tenant):
    """Crea un producto con stock bajo (stock < umbral efectivo de 5)."""
    from app.persistence.models.product import Product  # noqa: PLC0415

    p = Product(
        tenant_id=sample_tenant.tenant_id,
        name="Yerba Mate 500g",
        sale_price_ars=Decimal("1500.00"),
        unit_cost_ars=Decimal("900.00"),
        stock_units=2,
        low_stock_threshold_units=None,  # umbral efectivo = 5
        is_active=True,
    )
    db_session.add(p)
    await db_session.commit()
    return p


@pytest_asyncio.fixture
async def product_ok_stock(db_session: AsyncSession, sample_tenant: Tenant):
    """Crea un producto con stock saludable (por encima del umbral)."""
    from app.persistence.models.product import Product  # noqa: PLC0415

    p = Product(
        tenant_id=sample_tenant.tenant_id,
        name="Coca Cola 500ml",
        sale_price_ars=Decimal("800.00"),
        unit_cost_ars=Decimal("400.00"),
        stock_units=20,
        low_stock_threshold_units=5,
        is_active=True,
    )
    db_session.add(p)
    await db_session.commit()
    return p


# ── Tests de modelo y migración ───────────────────────────────────────────────


async def test_purchase_order_model_migration(
    db_session: AsyncSession,
    sample_tenant: Tenant,
    supplier_fixture: Supplier,
) -> None:
    """Crear PO, persistir, releer; items round-trip JSONB."""
    # Guardar IDs antes de expire_all() para no disparar lazy loading síncrono
    tenant_id = sample_tenant.tenant_id
    supplier_id = supplier_fixture.id

    items_data = [
        {
            "product_id": str(uuid.uuid4()),
            "product_name": "Yerba 500g",
            "sku": None,
            "quantity": 10,
            "unit_cost": "900.00",
            "subtotal": "9000.00",
        }
    ]
    po = PurchaseOrder(
        tenant_id=tenant_id,
        supplier_id=supplier_id,
        status="draft",
        total=Decimal("9000.00"),
        items=items_data,
        notes="Pedido test",
    )
    db_session.add(po)
    await db_session.flush()
    po_id = po.id

    await db_session.commit()
    db_session.expire_all()

    # Releer desde DB
    from sqlalchemy import select  # noqa: PLC0415

    result = await db_session.execute(select(PurchaseOrder).where(PurchaseOrder.id == po_id))
    reloaded = result.scalar_one()

    assert reloaded.status == "draft"
    assert reloaded.total == Decimal("9000.00")
    assert reloaded.notes == "Pedido test"
    assert isinstance(reloaded.items, list)
    assert len(reloaded.items) == 1
    assert reloaded.items[0]["product_name"] == "Yerba 500g"
    assert reloaded.items[0]["quantity"] == 10
    assert reloaded.supplier_id == supplier_id
    assert reloaded.tenant_id == tenant_id


# ── Tests de catalog_url ──────────────────────────────────────────────────────


async def test_supplier_catalog_url_persisted(
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    """catalog_url y api_url persisten y vuelven en SupplierResponse."""
    from app.schemas.supplier import SupplierResponse  # noqa: PLC0415

    sup = Supplier(
        tenant_id=sample_tenant.tenant_id,
        name="Proveedor Web",
        catalog_url="https://catalogo.proveedor.com",
        api_url="https://api.proveedor.com/v1",
    )
    db_session.add(sup)
    await db_session.commit()
    await db_session.refresh(sup)

    assert sup.catalog_url == "https://catalogo.proveedor.com"
    assert sup.api_url == "https://api.proveedor.com/v1"

    # Schema round-trip
    response = SupplierResponse.model_validate(sup)
    assert response.catalog_url == "https://catalogo.proveedor.com"
    assert response.api_url == "https://api.proveedor.com/v1"


async def test_supplier_catalog_url_nullable(
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    """catalog_url y api_url son opcionales (nullable)."""
    sup = Supplier(
        tenant_id=sample_tenant.tenant_id,
        name="Proveedor Sin Catálogo",
    )
    db_session.add(sup)
    await db_session.commit()
    await db_session.refresh(sup)

    assert sup.catalog_url is None
    assert sup.api_url is None


# ── Tests cross-tenant ────────────────────────────────────────────────────────


async def test_purchase_order_cross_tenant(
    db_session: AsyncSession,
    sample_tenant: Tenant,
    second_tenant: Tenant,
) -> None:
    """list_by_tenant no devuelve POs de otro tenant — aislamiento estricto."""
    po_tenant1 = PurchaseOrder(
        tenant_id=sample_tenant.tenant_id,
        status="draft",
        total=Decimal("5000.00"),
        items=[],
    )
    po_tenant2 = PurchaseOrder(
        tenant_id=second_tenant.tenant_id,
        status="draft",
        total=Decimal("3000.00"),
        items=[],
    )
    db_session.add_all([po_tenant1, po_tenant2])
    await db_session.commit()

    repo = PurchaseOrderRepository(db_session)

    # Tenant 1 solo ve su PO
    pos_t1 = await repo.list_by_tenant(sample_tenant.tenant_id)
    assert len(pos_t1) == 1
    assert pos_t1[0].tenant_id == sample_tenant.tenant_id

    # Tenant 2 solo ve su PO
    pos_t2 = await repo.list_by_tenant(second_tenant.tenant_id)
    assert len(pos_t2) == 1
    assert pos_t2[0].tenant_id == second_tenant.tenant_id


async def test_purchase_order_list_by_supplier_cross_tenant(
    db_session: AsyncSession,
    sample_tenant: Tenant,
    second_tenant: Tenant,
) -> None:
    """list_by_supplier filtra por tenant_id además de supplier_id."""
    # Crear proveedores con el mismo id simulado (en realidad diferentes)
    sup1 = Supplier(tenant_id=sample_tenant.tenant_id, name="Norte")
    sup2 = Supplier(tenant_id=second_tenant.tenant_id, name="Sur")
    db_session.add_all([sup1, sup2])
    await db_session.flush()

    po1 = PurchaseOrder(
        tenant_id=sample_tenant.tenant_id,
        supplier_id=sup1.id,
        status="draft",
        total=Decimal("1000.00"),
        items=[],
    )
    po2 = PurchaseOrder(
        tenant_id=second_tenant.tenant_id,
        supplier_id=sup2.id,
        status="draft",
        total=Decimal("2000.00"),
        items=[],
    )
    db_session.add_all([po1, po2])
    await db_session.commit()

    repo = PurchaseOrderRepository(db_session)

    # Buscar los POs del proveedor sup1 pero con el tenant2 → vacío
    pos_wrong_tenant = await repo.list_by_supplier(sup1.id, second_tenant.tenant_id)
    assert pos_wrong_tenant == []

    # Buscar los POs del proveedor sup1 con el tenant correcto → 1 resultado
    pos_correct = await repo.list_by_supplier(sup1.id, sample_tenant.tenant_id)
    assert len(pos_correct) == 1
    assert pos_correct[0].supplier_id == sup1.id


# ── Tests de routing ──────────────────────────────────────────────────────────


def test_purchase_order_routing() -> None:
    """preparar_pedido_sugerido rutea a agent_supplier con CREATE_PURCHASE_SUGGESTION."""
    assert (
        INTENT_TO_ACTION_TYPE["preparar_pedido_sugerido"] == ActionType.CREATE_PURCHASE_SUGGESTION
    )
    assert INTENT_TO_AGENT["preparar_pedido_sugerido"] == "agent_supplier"


def test_analizar_proveedores_routing_unchanged() -> None:
    """analizar_proveedores sigue ruteando a ANALYZE_SUPPLIER_DATA (no cambió)."""
    assert INTENT_TO_ACTION_TYPE["analizar_proveedores"] == ActionType.ANALYZE_SUPPLIER_DATA
    assert INTENT_TO_AGENT["analizar_proveedores"] == "agent_supplier"


# ── Tests del handler del agente ──────────────────────────────────────────────


async def test_agent_supplier_purchase_order_creates_draft(
    db_session: AsyncSession,
    sample_tenant: Tenant,
    product_low_stock,
    supplier_fixture: Supplier,
) -> None:
    """Con productos en quiebre, el agente crea un PurchaseOrder(status='draft')."""
    from unittest.mock import AsyncMock, patch  # noqa: PLC0415

    from app.application.agents.shared.schemas import AgentTask  # noqa: PLC0415
    from app.application.agents.supplier.agent import AgentSupplier  # noqa: PLC0415

    # Simular que hay ventas del supplier_fixture (para que _supplier_totals lo devuelva)
    # Usamos mock de _supplier_totals para no depender de ventas/gastos en la DB
    agent = AgentSupplier(session=db_session)
    request = AgentRequest(
        request_id="req-001",
        user_id=str(uuid.uuid4()),
        business_id=str(sample_tenant.tenant_id),
        message="Armame un pedido al proveedor",
        attachments=[],
        conversation_id=str(uuid.uuid4()),
    )
    task = AgentTask(
        task_id="task-001",
        agent="agent_supplier",
        action_type=ActionType.CREATE_PURCHASE_SUGGESTION,
        entities={},
    )

    # Mock _supplier_totals para devolver el supplier que ya está en DB
    mock_totals = [
        {
            "name": "Distribuidora Norte",
            "total": 50000.0,
            "count": 10,
            "last_purchase": "2026-07-01",
            "days_since": 23,
            "pct": 100.0,
        }
    ]

    with patch.object(agent, "_supplier_totals", AsyncMock(return_value=mock_totals)):
        response = await agent.process(request, task)

    assert response.status == "success"
    assert response.risk_level.value == "LOW"
    assert response.requires_approval is False

    # Debe haber creado un PO
    po_repo = PurchaseOrderRepository(db_session)
    pos = await po_repo.list_by_tenant(sample_tenant.tenant_id)
    assert len(pos) == 1
    po = pos[0]
    assert po.status == "draft"
    assert po.tenant_id == sample_tenant.tenant_id

    # Verificar items y total con Decimal
    assert len(po.items) == 1
    item = po.items[0]
    assert item["product_name"] == "Yerba Mate 500g"
    # quantity = max(5 - 2, 1) = 3
    assert item["quantity"] == 3
    # unit_cost = 900.00, subtotal = 3 * 900 = 2700.00
    assert Decimal(str(item["subtotal"])) == Decimal("2700.00")
    assert po.total == Decimal("2700.00")

    # Verificar que el PO referencia el supplier correcto
    assert po.supplier_id == supplier_fixture.id

    # Verificar que el response contiene el purchase_order_id
    assert "purchase_order_id" in response.result["structured_data"]
    assert response.result["structured_data"]["purchase_order_id"] == str(po.id)


async def test_agent_supplier_purchase_order_no_quiebres(
    db_session: AsyncSession,
    sample_tenant: Tenant,
    product_ok_stock,
) -> None:
    """Sin productos en quiebre, no se crea PO y el mensaje es claro."""
    from app.application.agents.shared.schemas import AgentTask  # noqa: PLC0415
    from app.application.agents.supplier.agent import AgentSupplier  # noqa: PLC0415

    agent = AgentSupplier(session=db_session)
    request = AgentRequest(
        request_id="req-002",
        user_id=str(uuid.uuid4()),
        business_id=str(sample_tenant.tenant_id),
        message="Armame un pedido",
        attachments=[],
        conversation_id=str(uuid.uuid4()),
    )
    task = AgentTask(
        task_id="task-002",
        agent="agent_supplier",
        action_type=ActionType.CREATE_PURCHASE_SUGGESTION,
        entities={},
    )

    response = await agent.process(request, task)

    assert response.status == "success"
    assert "quiebre" in (response.message or "").lower() or "pedido_sin_quiebres" in (
        response.result or {}
    ).get("summary", "")

    # No debe haber creado PO
    po_repo = PurchaseOrderRepository(db_session)
    pos = await po_repo.list_by_tenant(sample_tenant.tenant_id)
    assert pos == []


async def test_agent_supplier_critical_stock_extended_keys(
    db_session: AsyncSession,
    sample_tenant: Tenant,
    product_low_stock,
) -> None:
    """_critical_stock_for_order incluye las claves extendidas (F3a) sin romper las originales."""
    from app.application.agents.supplier.agent import AgentSupplier  # noqa: PLC0415

    agent = AgentSupplier(session=db_session)
    critical = await agent._critical_stock_for_order(sample_tenant.tenant_id)

    assert len(critical) == 1
    item = critical[0]

    # Claves originales (no deben romperse)
    assert "name" in item
    assert "stock" in item
    assert item["name"] == "Yerba Mate 500g"
    assert item["stock"] == 2

    # Claves nuevas F3a
    assert "product_id" in item
    assert "unit_cost" in item
    assert "threshold" in item
    assert isinstance(item["unit_cost"], Decimal)
    assert item["unit_cost"] == Decimal("900.00")
    assert item["threshold"] == 5  # low_stock_threshold_units=None → default 5
