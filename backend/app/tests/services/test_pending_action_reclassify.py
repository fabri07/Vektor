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


@pytest.mark.asyncio
async def test_reclassify_nonexistent_raises_visible_error(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """0 resultados → ReclassifyExpenseError con user_message (FAILED visible)."""
    from app.application.services.pending_action_service import ReclassifyExpenseError

    tenant_id = sample_tenant.tenant_id
    action = _make_action(
        tenant_id,
        {
            "expense_id": str(uuid.uuid4()),  # no existe
            "target": "insumo",
            "category": "SUPPLIES",
        },
    )
    with pytest.raises(ReclassifyExpenseError) as exc_info:
        await execute_pending_action(action, db_session)
    assert hasattr(exc_info.value, "user_message")
    assert "encontr" in ReclassifyExpenseError.user_message.lower()


@pytest.mark.asyncio
async def test_reclassify_massive_expense_ids(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """expense_ids[] reclasifica varios gastos en una sola acción."""
    tenant_id = sample_tenant.tenant_id
    e1 = await _make_expense(db_session, tenant_id, description="revista uno")
    e2 = await _make_expense(db_session, tenant_id, description="revista dos")

    action = _make_action(
        tenant_id,
        {
            "expense_ids": [str(e1.id), str(e2.id)],
            "target": "insumo",
            "category": "SUPPLIES",
        },
    )
    await execute_pending_action(action, db_session)
    await db_session.flush()
    await db_session.refresh(e1)
    await db_session.refresh(e2)

    assert e1.category == "SUPPLIES"
    assert e1.expense_type == "OPEX"
    assert e2.category == "SUPPLIES"
    assert e2.expense_type == "OPEX"


# ── Búsqueda asistida + reclasificación masiva (Workstream C3) ────────────────


@pytest.mark.asyncio
async def test_search_reclassify_with_clear_target_builds_massive_pending(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """"detectá los registros de revistas" + target → pending masivo (expense_ids)."""
    from app.application.agents.expense.agent import AgentExpense
    from app.application.agents.shared.schemas import AgentRequest, AgentTask, RiskLevel

    tenant_id = sample_tenant.tenant_id
    await _make_expense(db_session, tenant_id, description="revista La Nación")
    await _make_expense(db_session, tenant_id, description="revista Clarín")
    await _make_expense(db_session, tenant_id, description="alquiler local")

    agent = AgentExpense(db=db_session)
    req = AgentRequest(
        user_id=str(uuid.uuid4()),
        business_id=str(tenant_id),
        message="detectá los registros de revista para reclasificar",
    )
    task = AgentTask(
        agent="agent_expense",
        action_type=ActionType.RECLASSIFY_EXPENSE,
        entities={"search": "true", "target": "insumo"},
    )
    res = await agent.process(req, task=task)
    assert res.status == "requires_approval"
    assert res.risk_level == RiskLevel.MEDIUM
    sd = res.result["structured_data"]
    assert len(sd["expense_ids"]) == 2  # solo las dos revistas
    assert sd["target"] == "insumo"


@pytest.mark.asyncio
async def test_search_reclassify_no_matches_asks_clarification(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    from app.application.agents.expense.agent import AgentExpense
    from app.application.agents.shared.schemas import AgentRequest, AgentTask

    tenant_id = sample_tenant.tenant_id
    await _make_expense(db_session, tenant_id, description="alquiler local")

    agent = AgentExpense(db=db_session)
    req = AgentRequest(
        user_id=str(uuid.uuid4()),
        business_id=str(tenant_id),
        message="detectá los registros de cigarrillos para reclasificar",
    )
    task = AgentTask(
        agent="agent_expense",
        action_type=ActionType.RECLASSIFY_EXPENSE,
        entities={"search": "true", "target": "reventa"},
    )
    res = await agent.process(req, task=task)
    assert res.status == "requires_clarification"
    assert res.result["candidates"] == []
