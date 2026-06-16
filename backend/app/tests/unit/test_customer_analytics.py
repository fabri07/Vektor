"""Tests de la familia Clientes de AgentIncome (Fase 2 — chat).

Cubre:
- Funciones puras de `shared/analytics.py`: rank_customers, summarize_receivables.
- Handler `analizar_clientes` (general) con clientes + ventas → nombres y montos.
- Sub-análisis cuentas_por_cobrar (fiado) e inactivos.
- Caso sin datos (no-invention): clientes sin ventas / sin clientes cargados.
"""

import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.agents.shared import analytics
from app.application.agents.income.agent import AgentIncome
from app.application.agents.shared.schemas import ActionType, AgentRequest, AgentTask
from app.persistence.models.customer import Customer
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import SaleEntry


# ── Funciones puras ───────────────────────────────────────────────────────────


def test_rank_customers_orders_and_computes_avg_ticket():
    rows = [
        {"customer_id": "a", "customer_name": "Ana", "total": 3000.0, "n_sales": 3},
        {"customer_id": "b", "customer_name": "Beto", "total": 10000.0, "n_sales": 2},
        {"customer_id": "c", "customer_name": "Caro", "total": 0.0, "n_sales": 0},
    ]
    out = analytics.rank_customers(rows, top_n=2)
    assert [c["customer_name"] for c in out["top"]] == ["Beto", "Ana"]
    assert out["top"][0]["avg_ticket"] == 5000.0  # 10000 / 2
    assert out["n_customers"] == 3
    assert out["total_revenue"] == 13000.0
    assert out["total_sales"] == 5


def test_rank_customers_zero_sales_avg_ticket_none():
    rows = [{"customer_id": "c", "customer_name": "Caro", "total": 0.0, "n_sales": 0}]
    out = analytics.rank_customers(rows)
    assert out["top"][0]["avg_ticket"] is None  # no se inventa


def test_summarize_receivables_totals_and_order():
    rows = [
        {"customer_id": "a", "customer_name": "Ana", "total_owed": 500.0, "n_sales": 1},
        {"customer_id": "b", "customer_name": "Beto", "total_owed": 1500.0, "n_sales": 2},
    ]
    out = analytics.summarize_receivables(rows)
    assert out["total_owed"] == 2000.0
    assert out["by_customer"][0]["customer_name"] == "Beto"  # más debe primero
    assert out["n_customers"] == 2


def test_summarize_receivables_empty():
    out = analytics.summarize_receivables([])
    assert out["total_owed"] == 0.0
    assert out["by_customer"] == []


# ── Handler con DB ────────────────────────────────────────────────────────────


def _task(intent: str) -> AgentTask:
    return AgentTask(
        agent="agent_income",
        action_type=ActionType.ANALYZE_SALES_DATA,
        entities={"_intent": intent},
    )


def _req(tenant_id: uuid.UUID, msg: str = "") -> AgentRequest:
    return AgentRequest(user_id=str(uuid.uuid4()), business_id=str(tenant_id), message=msg)


async def _mk_tenant(db_session: AsyncSession) -> uuid.UUID:
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
    await db_session.commit()
    return tid


@pytest_asyncio.fixture
async def seeded_customers(db_session: AsyncSession) -> uuid.UUID:
    tid = await _mk_tenant(db_session)
    ana = Customer(tenant_id=tid, name="Ana")
    beto = Customer(tenant_id=tid, name="Beto")
    inactivo = Customer(tenant_id=tid, name="Carlos Inactivo")
    db_session.add_all([ana, beto, inactivo])
    await db_session.commit()

    today = date.today()
    # Beto: 2 ventas, una al contado y una fiado (account)
    db_session.add(
        SaleEntry(
            tenant_id=tid,
            customer_id=beto.id,
            amount=Decimal("8000"),
            quantity=1,
            transaction_date=today,
            payment_method="cash",
        )
    )
    db_session.add(
        SaleEntry(
            tenant_id=tid,
            customer_id=beto.id,
            amount=Decimal("2000"),
            quantity=1,
            transaction_date=today,
            payment_method="account",
        )
    )
    # Ana: 1 venta al contado
    db_session.add(
        SaleEntry(
            tenant_id=tid,
            customer_id=ana.id,
            amount=Decimal("3000"),
            quantity=1,
            transaction_date=today,
            payment_method="cash",
        )
    )
    # Carlos: última venta hace 90 días → inactivo (>60)
    db_session.add(
        SaleEntry(
            tenant_id=tid,
            customer_id=inactivo.id,
            amount=Decimal("1000"),
            quantity=1,
            transaction_date=today - timedelta(days=90),
            payment_method="cash",
        )
    )
    await db_session.commit()
    return tid


@pytest.mark.asyncio
async def test_analizar_clientes_general(db_session, seeded_customers):
    agent = AgentIncome(db=db_session)
    resp = await agent.process(_req(seeded_customers), task=_task("analizar_clientes"))
    assert resp.status == "success"
    assert resp.result["summary"] == "analizar_clientes"
    # Beto factura más (10.000) que Ana (3.000) → aparece primero y con su monto.
    assert "Beto" in resp.message
    assert "Ana" in resp.message
    top = resp.result["structured_data"]["top"]
    assert top[0]["customer_name"] == "Beto"
    assert top[0]["total"] == 10000.0
    assert top[0]["n_sales"] == 2
    assert top[0]["avg_ticket"] == 5000.0


@pytest.mark.asyncio
async def test_cuentas_por_cobrar(db_session, seeded_customers):
    agent = AgentIncome(db=db_session)
    resp = await agent.process(
        _req(seeded_customers), task=_task("analizar_cuentas_por_cobrar")
    )
    assert resp.status == "success"
    assert resp.result["summary"] == "analizar_cuentas_por_cobrar"
    # Solo Beto tiene fiado (account) por 2.000.
    assert resp.result["structured_data"]["total_owed"] == 2000.0
    assert "Beto" in resp.message


@pytest.mark.asyncio
async def test_priorizar_cobranza(db_session, seeded_customers):
    agent = AgentIncome(db=db_session)
    resp = await agent.process(_req(seeded_customers), task=_task("priorizar_cobranza"))
    assert resp.status == "success"
    assert resp.result["summary"] == "priorizar_cobranza"
    assert "Beto" in resp.message


@pytest.mark.asyncio
async def test_clientes_inactivos(db_session, seeded_customers):
    agent = AgentIncome(db=db_session)
    resp = await agent.process(
        _req(seeded_customers), task=_task("detectar_clientes_inactivos")
    )
    assert resp.status == "success"
    assert resp.result["summary"] == "detectar_clientes_inactivos"
    # Carlos compró hace 90 días → inactivo; Ana/Beto compraron hoy → no.
    assert "Carlos Inactivo" in resp.message
    assert "Ana" not in resp.message
    inactive_names = {c["customer_name"] for c in resp.result["structured_data"]["inactive"]}
    assert "Carlos Inactivo" in inactive_names


@pytest.mark.asyncio
async def test_clientes_sin_clientes_cargados(db_session):
    tid = await _mk_tenant(db_session)
    agent = AgentIncome(db=db_session)
    resp = await agent.process(_req(tid), task=_task("analizar_clientes"))
    assert resp.status == "success"
    assert resp.result["summary"] == "clientes_sin_datos"


@pytest.mark.asyncio
async def test_clientes_cargados_sin_ventas(db_session):
    tid = await _mk_tenant(db_session)
    db_session.add(Customer(tenant_id=tid, name="Sin Compras"))
    await db_session.commit()
    agent = AgentIncome(db=db_session)
    resp = await agent.process(_req(tid), task=_task("analizar_clientes"))
    assert resp.status == "success"
    assert resp.result["summary"] == "clientes_sin_ventas"


@pytest.mark.asyncio
async def test_clientes_sin_db():
    """Sin DB → mensaje claro, sin crash (no-invention)."""
    agent = AgentIncome(db=None)
    resp = await agent.process(
        _req(uuid.uuid4()), task=_task("analizar_clientes")
    )
    assert resp.status == "success"
    assert resp.result["summary"] == "clientes_sin_datos"
