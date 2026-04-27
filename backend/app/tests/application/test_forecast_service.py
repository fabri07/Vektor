from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.forecast_service import get_forecast
from app.persistence.models.transaction import ExpenseEntry, SaleEntry


class FakeRedis:
    def __init__(self) -> None:
        self._store: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self._store.get(key)

    async def setex(self, key: str, ttl: int, value: str) -> None:
        self._store[key] = value


async def _seed_daily_cashflow(
    session: AsyncSession,
    tenant_id,
    days: int,
    *,
    end_date: date | None = None,
) -> None:
    last_day = end_date or date.today()
    first_day = last_day - timedelta(days=days - 1)
    for index in range(days):
        day = first_day + timedelta(days=index)
        session.add(
            SaleEntry(
                tenant_id=tenant_id,
                amount=Decimal("1000.00"),
                quantity=1,
                transaction_date=day,
                payment_method="cash",
            )
        )
        session.add(
            ExpenseEntry(
                tenant_id=tenant_id,
                amount=Decimal("600.00"),
                category="OTHER",
                transaction_date=day,
                description="Gasto diario",
                is_recurring=False,
                payment_method="cash",
            )
        )
    await session.commit()


@pytest.mark.asyncio
async def test_forecast_tier_0_with_less_than_14_days(db_session, sample_tenant) -> None:
    await _seed_daily_cashflow(db_session, sample_tenant.tenant_id, 7)

    result = await get_forecast(sample_tenant.tenant_id, db_session, FakeRedis(), True)

    assert result.tier == 0
    assert result.horizon_days == 0
    assert result.points == []


@pytest.mark.asyncio
async def test_forecast_tier_1_between_14_and_30_days(db_session, sample_tenant) -> None:
    await _seed_daily_cashflow(db_session, sample_tenant.tenant_id, 20)

    result = await get_forecast(sample_tenant.tenant_id, db_session, FakeRedis(), True)

    assert result.tier == 1
    assert result.confidence == "LOW"
    assert result.horizon_days == 14
    assert len(result.points) == 14


@pytest.mark.asyncio
async def test_forecast_tier_2_between_30_and_90_days(db_session, sample_tenant) -> None:
    await _seed_daily_cashflow(db_session, sample_tenant.tenant_id, 45)

    result = await get_forecast(sample_tenant.tenant_id, db_session, FakeRedis(), True)

    assert result.tier == 2
    assert result.confidence == "MEDIUM"
    assert result.horizon_days == 30
    assert len(result.points) == 30


@pytest.mark.asyncio
async def test_forecast_tier_3_after_90_days(db_session, sample_tenant) -> None:
    await _seed_daily_cashflow(db_session, sample_tenant.tenant_id, 120)

    result = await get_forecast(sample_tenant.tenant_id, db_session, FakeRedis(), True)

    assert result.tier == 3
    assert result.confidence == "HIGH"
    assert result.horizon_days == 60
    assert len(result.points) == 60


@pytest.mark.asyncio
async def test_forecast_marks_stale_data_as_low_confidence(db_session, sample_tenant) -> None:
    stale_end = date.today() - timedelta(days=10)
    await _seed_daily_cashflow(db_session, sample_tenant.tenant_id, 45, end_date=stale_end)

    result = await get_forecast(sample_tenant.tenant_id, db_session, FakeRedis(), True)

    assert result.tier == 2
    assert result.confidence == "LOW"
    assert result.message is not None
