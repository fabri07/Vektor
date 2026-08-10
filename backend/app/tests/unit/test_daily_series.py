"""Tests de las series diarias del repo: revenue, expense y neta (caja).

Verifica:
- Zero-fill: días sin movimiento = 0.0, largo == days.
- Orden cronológico: índice 0 = día más viejo, índice -1 = hoy.
- Exclusión de filas con voided_at.
- get_daily_net_series: rev[i] - exp[i] alineado día a día con rango único.
"""
from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import ExpenseEntry, SaleEntry
from app.persistence.repositories.transaction_repository import (
    ExpenseRepository,
    SaleRepository,
    get_daily_net_series,
)


@pytest_asyncio.fixture
async def tenant_id(db_session: AsyncSession) -> uuid.UUID:
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


def _sale(tid: uuid.UUID, amount: str, days_ago: int, *, voided: bool = False) -> SaleEntry:
    return SaleEntry(
        tenant_id=tid,
        amount=Decimal(amount),
        quantity=1,
        transaction_date=datetime.combine(
            date.today() - timedelta(days=days_ago), datetime.min.time()
        ),
        payment_method="cash",
        voided_at=datetime.now(UTC) if voided else None,
        void_reason="USER_CANCELLED" if voided else None,
    )


def _expense(tid: uuid.UUID, amount: str, days_ago: int) -> ExpenseEntry:
    return ExpenseEntry(
        tenant_id=tid,
        amount=Decimal(amount),
        category="OTHER",
        description="test",
        transaction_date=datetime.combine(
            date.today() - timedelta(days=days_ago), datetime.min.time()
        ),
        payment_method="cash",
    )


async def test_revenue_series_zero_fill_and_order(
    db_session: AsyncSession, tenant_id: uuid.UUID
) -> None:
    # Ventas hoy (idx 6), hace 2 días (idx 4), hace 6 días (idx 0) en ventana de 7
    db_session.add_all(
        [
            _sale(tenant_id, "100", 0),
            _sale(tenant_id, "200", 2),
            _sale(tenant_id, "300", 6),
        ]
    )
    await db_session.commit()

    series = await SaleRepository(db_session).get_daily_revenue_series(tenant_id, days=7)
    assert len(series) == 7
    assert series[0] == 300.0  # más viejo
    assert series[4] == 200.0
    assert series[6] == 100.0  # hoy
    assert series[1] == 0.0  # día sin venta → zero-fill
    assert series[2] == 0.0


async def test_revenue_series_excludes_voided(
    db_session: AsyncSession, tenant_id: uuid.UUID
) -> None:
    db_session.add_all(
        [
            _sale(tenant_id, "100", 1),
            _sale(tenant_id, "999", 1, voided=True),
        ]
    )
    await db_session.commit()

    series = await SaleRepository(db_session).get_daily_revenue_series(tenant_id, days=7)
    # idx 5 = ayer; solo la venta no anulada
    assert series[5] == 100.0


async def test_revenue_series_same_day_summed(
    db_session: AsyncSession, tenant_id: uuid.UUID
) -> None:
    db_session.add_all([_sale(tenant_id, "100", 3), _sale(tenant_id, "50", 3)])
    await db_session.commit()

    series = await SaleRepository(db_session).get_daily_revenue_series(tenant_id, days=7)
    assert series[3] == 150.0  # 7 días → idx 3 = hace 3 días


async def test_expense_series_zero_fill(
    db_session: AsyncSession, tenant_id: uuid.UUID
) -> None:
    db_session.add_all([_expense(tenant_id, "80", 0), _expense(tenant_id, "20", 5)])
    await db_session.commit()

    series = await ExpenseRepository(db_session).get_daily_expense_series(tenant_id, days=7)
    assert len(series) == 7
    assert series[6] == 80.0  # hoy
    assert series[1] == 20.0  # hace 5 días → idx 1
    assert series[3] == 0.0


async def test_net_series_aligned(db_session: AsyncSession, tenant_id: uuid.UUID) -> None:
    # hoy: venta 100, gasto 30 → neto 70 (idx 6)
    # ayer: solo gasto 50 → neto -50 (idx 5)
    db_session.add_all(
        [
            _sale(tenant_id, "100", 0),
            _expense(tenant_id, "30", 0),
            _expense(tenant_id, "50", 1),
        ]
    )
    await db_session.commit()

    series = await get_daily_net_series(db_session, tenant_id, days=7)
    assert len(series) == 7
    assert series[6] == 70.0
    assert series[5] == -50.0
    assert series[0] == 0.0


async def test_empty_series_all_zeros(
    db_session: AsyncSession, tenant_id: uuid.UUID
) -> None:
    series = await SaleRepository(db_session).get_daily_revenue_series(tenant_id, days=10)
    assert series == [0.0] * 10
