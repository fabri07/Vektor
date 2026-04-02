"""Integration tests for cash workflows — SQLite in-memory, no Celery/Redis."""

import unittest.mock
import uuid
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.models.transaction import ExpenseEntry, SaleEntry
from app.tests.integration.conftest import make_tenant, make_user


@pytest_asyncio.fixture
async def tenant(session: AsyncSession):
    return await make_tenant(session, legal_name="Kiosco Test Cash")


@pytest_asyncio.fixture
async def user(session: AsyncSession, tenant):
    return await make_user(session, tenant.tenant_id)


@pytest.mark.asyncio
async def test_full_sale_workflow(session: AsyncSession, tenant, user):
    """save_sale → SaleEntry persiste en DB con el payment_method correcto."""
    from app.application.services.cash_service import save_sale

    entities = {
        "amount": 3500,
        "payment_status": "paid",
        "payment_method": "efectivo",
        "product_description": "Gaseosa 2L",
    }

    with unittest.mock.patch(
        "app.application.services.cash_service.logger"
    ):
        sale = await save_sale(entities, tenant.tenant_id, user.user_id, session)
        await session.commit()

    result = await session.execute(
        select(SaleEntry).where(
            SaleEntry.tenant_id == tenant.tenant_id,
            SaleEntry.id == sale.id,
        )
    )
    entry = result.scalar_one()

    assert entry.amount == Decimal("3500")
    assert entry.payment_method == "efectivo"
    assert entry.notes == "Gaseosa 2L"


@pytest.mark.asyncio
async def test_sale_not_in_db_before_confirm(session: AsyncSession, tenant, user):
    """Antes de ejecutar save_sale no hay SaleEntry para el tenant."""
    result = await session.execute(
        select(SaleEntry).where(SaleEntry.tenant_id == tenant.tenant_id)
    )
    entries = result.scalars().all()
    assert len(entries) == 0


@pytest.mark.asyncio
async def test_cash_coverage_alert_generated(session: AsyncSession, tenant):
    """recalculate_cash_health emite CASH_HEALTH_UPDATED (alerta de cobertura)."""
    with unittest.mock.patch(
        "app.application.agents.cash.agent.anthropic.AsyncAnthropic"
    ):
        with unittest.mock.patch(
            "app.application.agents.cash.agent.EventBus.emit"
        ) as mock_emit:
            from app.application.agents.cash.agent import AgentCash

            agent = AgentCash()
            await agent.recalculate_cash_health(str(tenant.tenant_id))

    mock_emit.assert_called_once_with(
        "CASH_HEALTH_UPDATED", {"business_id": str(tenant.tenant_id)}
    )
