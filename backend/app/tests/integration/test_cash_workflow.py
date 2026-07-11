"""Integration tests for cash workflows — SQLite in-memory, no Celery/Redis."""

import unittest.mock
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.models.transaction import SaleEntry
from app.tests.integration.conftest import make_tenant, make_user


@pytest_asyncio.fixture
async def tenant(session: AsyncSession):
    return await make_tenant(session, legal_name="Kiosco Test Cash")


@pytest_asyncio.fixture
async def user(session: AsyncSession, tenant):
    return await make_user(session, tenant.tenant_id)


@pytest.mark.asyncio
async def test_full_sale_workflow(session: AsyncSession, tenant, user) -> None:
    """save_sale → SaleEntry persiste en DB con el payment_method correcto."""
    from app.application.services.cash_service import save_sale

    entities = {
        "amount": 3500,
        "payment_status": "paid",
        "payment_method": "efectivo",
        "product_description": "Gaseosa 2L",
    }

    with unittest.mock.patch("app.application.services.cash_service.logger"):
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
    assert entry.payment_method == "cash"
    assert entry.notes == "Gaseosa 2L"


@pytest.mark.asyncio
async def test_save_sale_requires_explicit_payment_method(
    session: AsyncSession, tenant, user
) -> None:
    """save_sale no debe asumir cash si falta el método de pago."""
    from app.application.services.cash_service import save_sale

    entities = {
        "amount": 3500,
        "payment_status": "paid",
        "product_description": "Gaseosa 2L",
    }

    with pytest.raises(ValueError, match="payment_method_required_for_sale"):
        await save_sale(entities, tenant.tenant_id, user.user_id, session)


@pytest.mark.asyncio
async def test_sale_not_in_db_before_confirm(session: AsyncSession, tenant, user) -> None:
    """Antes de ejecutar save_sale no hay SaleEntry para el tenant."""
    result = await session.execute(select(SaleEntry).where(SaleEntry.tenant_id == tenant.tenant_id))
    entries = result.scalars().all()
    assert len(entries) == 0


@pytest.mark.asyncio
async def test_sale_confirmed_emits_event(session: AsyncSession, tenant) -> None:
    """on_confirmed_sale emite SALE_RECORDED vía EventBus."""
    with (
        unittest.mock.patch("app.application.agents.cash.agent.anthropic.AsyncAnthropic"),
        unittest.mock.patch("app.application.agents.cash.agent.EventBus.emit") as mock_emit,
    ):
        from app.application.agents.cash.agent import AgentCash

        agent = AgentCash()
        await agent.on_confirmed_sale("sale-123", str(tenant.tenant_id))

    mock_emit.assert_called_once_with(
        "SALE_RECORDED", {"sale_id": "sale-123", "tenant_id": str(tenant.tenant_id)}
    )
