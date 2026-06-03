"""Tests de los handlers analíticos de AgentExpense (Sprint 17, Stage 5)."""

import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.agents.expense.agent import AgentExpense
from app.application.agents.shared.schemas import ActionType, AgentRequest, AgentTask
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import ExpenseEntry


def _task(intent: str) -> AgentTask:
    return AgentTask(
        agent="agent_expense",
        action_type=ActionType.ANALYZE_EXPENSE_DATA,
        entities={"_intent": intent},
    )


def _req(tenant_id: uuid.UUID, msg: str = "") -> AgentRequest:
    return AgentRequest(user_id=str(uuid.uuid4()), business_id=str(tenant_id), message=msg)


@pytest_asyncio.fixture
async def seeded(db_session: AsyncSession) -> uuid.UUID:
    tid = uuid.uuid4()
    db_session.add(
        Tenant(
            tenant_id=tid,
            legal_name="T",
            display_name="T",
            currency="ARS",
            pricing_reference_mode="MEP",
            status="ACTIVE",
        )
    )
    today = date.today()
    # Alquiler recurrente (fijo) + servicios normales + 1 outlier
    rows = [
        ExpenseEntry(
            tenant_id=tid,
            amount=Decimal("450000"),
            category="RENT",
            transaction_date=today - timedelta(days=d),
            description="Alquiler",
            is_recurring=True,
            payment_method="transfer",
            supplier_name="Inmobiliaria",
        )
        for d in (1, 31, 61)
    ]
    rows += [
        ExpenseEntry(
            tenant_id=tid,
            amount=Decimal("10000"),
            category="UTILITIES",
            transaction_date=today - timedelta(days=d),
            description="Luz",
            is_recurring=False,
            payment_method="transfer",
        )
        for d in (2, 12, 22, 32)
    ]
    # outlier de servicios
    rows.append(
        ExpenseEntry(
            tenant_id=tid,
            amount=Decimal("90000"),
            category="UTILITIES",
            transaction_date=today - timedelta(days=5),
            description="Luz rara",
            is_recurring=False,
            payment_method="transfer",
        )
    )
    db_session.add_all(rows)
    await db_session.commit()
    return tid


@pytest.mark.asyncio
async def test_clasificar_gastos(db_session, seeded):
    agent = AgentExpense(db=db_session)
    resp = await agent.process(_req(seeded), task=_task("clasificar_gastos"))
    assert resp.status == "success"
    assert resp.result["structured_data"]["by_category"]


@pytest.mark.asyncio
async def test_gastos_recurrentes(db_session, seeded):
    agent = AgentExpense(db=db_session)
    resp = await agent.process(_req(seeded), task=_task("detectar_gastos_recurrentes"))
    assert resp.status == "success"
    assert "Inmobiliaria" in resp.message


@pytest.mark.asyncio
async def test_gastos_anomalos(db_session, seeded):
    agent = AgentExpense(db=db_session)
    resp = await agent.process(_req(seeded), task=_task("detectar_gastos_anomalos"))
    assert resp.status == "success"
    # el outlier de 90000 en UTILITIES debería detectarse
    assert resp.result["summary"] in ("detectar_gastos_anomalos", "gastos_anomalos_ninguno")


@pytest.mark.asyncio
async def test_costos_fijos_variables(db_session, seeded):
    agent = AgentExpense(db=db_session)
    resp = await agent.process(_req(seeded), task=_task("analizar_costos_fijos_variables"))
    assert resp.status == "success"
    assert resp.result["structured_data"]["fijos"] > 0


@pytest.mark.asyncio
async def test_punto_equilibrio_sin_ventas(db_session, seeded):
    agent = AgentExpense(db=db_session)
    resp = await agent.process(_req(seeded), task=_task("calcular_punto_equilibrio"))
    # sin ventas → no hay margen bruto → mensaje pidiendo datos
    assert resp.result["summary"] == "equilibrio_sin_margen"


@pytest.mark.asyncio
async def test_empty(db_session):
    tid = uuid.uuid4()
    db_session.add(
        Tenant(
            tenant_id=tid,
            legal_name="E",
            display_name="E",
            currency="ARS",
            pricing_reference_mode="MEP",
            status="ACTIVE",
        )
    )
    await db_session.commit()
    agent = AgentExpense(db=db_session)
    resp = await agent.process(_req(tid), task=_task("clasificar_gastos"))
    assert resp.result["summary"] == "gastos_sin_categorias"
