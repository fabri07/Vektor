"""Tests de ejecución de RECLASSIFY_EXPENSE en PendingActionService (Nivel 2).

Cubre:
- reventa: reclasifica category=INVENTORY + expense_type=COGS y crea/vincula un
  Product vendible incompleto (expense.product_id seteado).
- insumo: reclasifica category=SUPPLIES + expense_type=OPEX, NO crea producto.
- audit log con before/after (reversible).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.agents.shared.schemas import ActionType
from app.application.services.pending_action_service import execute_pending_action
from app.persistence.models.audit import DecisionAuditLog
from app.persistence.models.pending_action import PendingAction
from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import ExpenseEntry


async def _make_expense(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    category: str = "OTHER",
    expense_type: str = "OPEX",
    description: str = "compra varios",
) -> ExpenseEntry:
    expense = ExpenseEntry(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        amount=Decimal("12000"),
        category=category,
        expense_type=expense_type,
        transaction_date=datetime(2026, 6, 1, 10, 0, 0),
        description=description,
        payment_method="cash",
        provenance="REAL",
    )
    db.add(expense)
    await db.flush()
    return expense


def _make_action(tenant_id: uuid.UUID, payload: dict[str, object]) -> PendingAction:
    action = PendingAction()
    action.id = uuid.uuid4()
    action.tenant_id = tenant_id
    action.user_id = uuid.uuid4()
    action.action_type = ActionType.RECLASSIFY_EXPENSE
    action.payload = payload
    action.risk_level = "MEDIUM"
    action.status = "APPROVED"
    action.external_system = None
    return action


@pytest.mark.asyncio
async def test_reclassify_to_reventa_creates_product_and_cogs(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    tenant_id = sample_tenant.tenant_id
    expense = await _make_expense(db_session, tenant_id, description="caja de alfajores")

    action = _make_action(
        tenant_id,
        {
            "expense_id": str(expense.id),
            "target": "reventa",
            "sku": "ALF-001",
            "product_name": "Alfajores triple",
        },
    )
    await execute_pending_action(action, db_session)
    await db_session.flush()
    await db_session.refresh(expense)

    assert expense.category == "INVENTORY"
    assert expense.expense_type == "COGS"
    assert expense.product_id is not None

    product = await db_session.get(Product, expense.product_id)
    assert product is not None
    assert product.tenant_id == tenant_id
    assert product.requires_completion is True
    assert product.sale_price_ars == Decimal("0")
    assert product.sku == "ALF-001"
    assert product.name == "Alfajores triple"

    # Audit con before/after (reversible)
    audits = (
        (
            await db_session.execute(
                select(DecisionAuditLog).where(
                    DecisionAuditLog.tenant_id == tenant_id,
                    DecisionAuditLog.decision_type == "EXPENSE_RECLASSIFIED",
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(audits) == 1
    data = audits[0].decision_data
    assert data["before"]["category"] == "OTHER"
    assert data["after"]["category"] == "INVENTORY"
    assert data["after"]["expense_type"] == "COGS"


@pytest.mark.asyncio
async def test_reclassify_to_insumo_does_not_create_product(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    tenant_id = sample_tenant.tenant_id
    expense = await _make_expense(
        db_session, tenant_id, category="INVENTORY", expense_type="COGS", description="bolsas"
    )

    products_before = (
        (await db_session.execute(select(Product).where(Product.tenant_id == tenant_id)))
        .scalars()
        .all()
    )

    action = _make_action(
        tenant_id,
        {
            "expense_id": str(expense.id),
            "target": "insumo",
            "category": "SUPPLIES",
        },
    )
    await execute_pending_action(action, db_session)
    await db_session.flush()
    await db_session.refresh(expense)

    assert expense.category == "SUPPLIES"
    assert expense.expense_type == "OPEX"
    assert expense.product_id is None

    products_after = (
        (await db_session.execute(select(Product).where(Product.tenant_id == tenant_id)))
        .scalars()
        .all()
    )
    assert len(products_after) == len(products_before)


@pytest.mark.asyncio
async def test_reclassify_by_amount_and_date_fallback(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Sin expense_id: identifica por monto + fecha."""
    tenant_id = sample_tenant.tenant_id
    expense = await _make_expense(db_session, tenant_id, description="compra suelta")

    action = _make_action(
        tenant_id,
        {
            "monto": "12000",
            "fecha": "2026-06-01",
            "target": "insumo",
            "category": "SUPPLIES",
        },
    )
    await execute_pending_action(action, db_session)
    await db_session.flush()
    await db_session.refresh(expense)

    assert expense.category == "SUPPLIES"
    assert expense.expense_type == "OPEX"
