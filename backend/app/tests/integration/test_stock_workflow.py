"""Integration tests for stock workflows — SQLite in-memory, no Celery/Redis."""

import unittest.mock
import uuid
from decimal import Decimal
from unittest.mock import MagicMock

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.agents.shared.schemas import AgentRequest
from app.persistence.models.audit import DecisionAuditLog
from app.persistence.models.inventory import InventoryBalance
from app.persistence.models.product import Product
from app.tests.integration.conftest import make_tenant, make_user


@pytest_asyncio.fixture
async def tenant(session: AsyncSession):
    return await make_tenant(session, legal_name="Kiosco Test Stock")


@pytest_asyncio.fixture
async def user(session: AsyncSession, tenant):
    return await make_user(session, tenant.tenant_id)


@pytest_asyncio.fixture
async def product(session: AsyncSession, tenant):
    p = Product(
        tenant_id=tenant.tenant_id,
        name="Gaseosa 500ml",
        sale_price_ars=Decimal("500.00"),
        unit_cost_ars=Decimal("250.00"),
        stock_units=50,
        low_stock_threshold_units=5,
        is_active=True,
    )
    session.add(p)
    await session.commit()
    return p


@pytest.mark.asyncio
async def test_sale_decrements_stock(session: AsyncSession, tenant, product):
    """decrement_stock → inventory_balances decrementado correctamente."""
    from app.application.services.stock_service import decrement_stock

    with unittest.mock.patch(
        "app.application.services.stock_service.EventBus.emit"
    ):
        movement = await decrement_stock(
            product_id=product.id,
            tenant_id=tenant.tenant_id,
            qty=10,
            source_event_id="sale-001",
            db=session,
        )
        await session.commit()

    result = await session.execute(
        select(InventoryBalance).where(
            InventoryBalance.product_id == product.id,
            InventoryBalance.tenant_id == tenant.tenant_id,
        )
    )
    balance = result.scalar_one()

    assert balance.current_qty == 40  # 50 - 10
    assert movement.movement_type == "sale"
    assert movement.qty == -10


@pytest.mark.asyncio
async def test_stock_loss_creates_audit_entry(session: AsyncSession, tenant, product, user):
    """register_stock_loss → entrada en decision_audit_log con decision_type=STOCK_LOSS."""
    from app.application.services.stock_service import register_stock_loss

    with unittest.mock.patch(
        "app.application.services.stock_service.EventBus.emit"
    ):
        await register_stock_loss(
            product_id=product.id,
            tenant_id=tenant.tenant_id,
            qty=3,
            reason="vencimiento",
            actor_user_id=user.user_id,
            db=session,
        )
        await session.commit()

    result = await session.execute(
        select(DecisionAuditLog).where(
            DecisionAuditLog.tenant_id == tenant.tenant_id,
            DecisionAuditLog.decision_type == "STOCK_LOSS",
        )
    )
    audit = result.scalar_one()

    assert audit.decision_type == "STOCK_LOSS"
    assert audit.decision_data["qty_lost"] == 3
    assert audit.decision_data["reason"] == "vencimiento"


@pytest.mark.asyncio
async def test_bulk_adjustment_requires_confirmation(tenant, product):
    """AgentStock.process con 'ajuste' → status=requires_approval."""
    mock_entities = {
        "product_name": "Gaseosa",
        "sku": None,
        "qty_change": 100,
        "reason": "ajuste",
        "confidence": "HIGH",
    }

    content_block = MagicMock()
    content_block.text = __import__("json").dumps(mock_entities)
    mock_response = MagicMock()
    mock_response.content = [content_block]

    with unittest.mock.patch(
        "app.application.agents.stock.agent.anthropic.AsyncAnthropic"
    ) as mock_cls:
        from unittest.mock import AsyncMock

        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(return_value=mock_response)
        mock_cls.return_value = mock_client

        from app.application.agents.stock.agent import AgentStock

        agent = AgentStock()
        agent.client = mock_client

        request = AgentRequest(
            user_id=str(uuid.uuid4()),
            business_id=str(tenant.tenant_id),
            message="ajuste de 100 unidades de gaseosa",
        )
        result = await agent.process(request)

    assert result.status == "requires_approval"
    assert result.result["action_type"] == "UPDATE_STOCK"
