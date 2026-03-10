"""
Business State Layer (BSL).

Computes a normalized financial state snapshot for a tenant over a given
time window. This state is the ONLY input to the Health Engine — no raw
transaction data ever reaches the health score computation directly.

All calculations are pure aggregations; no scores are produced here.
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.repositories.transaction_repository import (
    ExpenseRepository,
    SaleRepository,
)


@dataclass
class BusinessState:
    """
    Normalized business state for a 30-day period.
    All score fields are 0–100 floats; explanation fields are human-readable.
    """

    tenant_id: UUID
    period_start: datetime
    period_end: datetime

    total_revenue: Decimal
    total_expenses: Decimal
    gross_profit: Decimal
    expense_ratio: Decimal        # expenses / revenue (lower = better)
    avg_daily_sales: Decimal
    transaction_count: int

    # Pre-computed dimension scores (0–100)
    liquidity_score: float
    liquidity_explanation: str

    profitability_score: float
    profitability_explanation: str

    cost_control_score: float
    cost_control_explanation: str

    sales_momentum_score: float
    sales_momentum_explanation: str

    debt_coverage_score: float
    debt_coverage_explanation: str


class BusinessStateLayer:
    """
    Computes the BusinessState from raw transaction data.
    Every score passes through this layer before the Health Engine.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def compute(
        self, tenant_id: UUID, period_start: datetime, period_end: datetime
    ) -> BusinessState:
        sale_repo = SaleRepository(self._session)
        expense_repo = ExpenseRepository(self._session)

        from_date = period_start.date()
        to_date = period_end.date()

        total_revenue = Decimal(
            str(await sale_repo.total_revenue(tenant_id, from_date, to_date))
        )
        total_expenses = Decimal(
            str(await expense_repo.total_expenses(tenant_id, from_date, to_date))
        )

        days = max((period_end - period_start).days, 1)
        gross_profit = total_revenue - total_expenses
        expense_ratio = (
            total_expenses / total_revenue if total_revenue > 0 else Decimal("1")
        )
        avg_daily_sales = total_revenue / Decimal(str(days))

        sales_list = await sale_repo.list_by_tenant(
            tenant_id, from_date=from_date, to_date=to_date, limit=1000
        )
        transaction_count = len(sales_list)

        # ── Dimension scores (simplified; extend with heuristic rules) ────────
        liq_score = self._liquidity_score(total_revenue, total_expenses)
        prof_score = self._profitability_score(gross_profit, total_revenue)
        cost_score = self._cost_control_score(expense_ratio)
        momentum_score = self._momentum_score(avg_daily_sales)
        debt_score = self._debt_coverage_score(gross_profit, total_expenses)

        return BusinessState(
            tenant_id=tenant_id,
            period_start=period_start,
            period_end=period_end,
            total_revenue=total_revenue,
            total_expenses=total_expenses,
            gross_profit=gross_profit,
            expense_ratio=expense_ratio,
            avg_daily_sales=avg_daily_sales,
            transaction_count=transaction_count,
            liquidity_score=liq_score,
            liquidity_explanation=f"Revenue {total_revenue:.2f} vs expenses {total_expenses:.2f} in {days} days.",
            profitability_score=prof_score,
            profitability_explanation=f"Gross profit margin: {(gross_profit / total_revenue * 100) if total_revenue else 0:.1f}%.",
            cost_control_score=cost_score,
            cost_control_explanation=f"Expense ratio: {expense_ratio * 100:.1f}% of revenue.",
            sales_momentum_score=momentum_score,
            sales_momentum_explanation=f"Average daily sales: {avg_daily_sales:.2f} ARS.",
            debt_coverage_score=debt_score,
            debt_coverage_explanation=f"Gross profit covers {(gross_profit / total_expenses * 100) if total_expenses else 100:.1f}% of expenses.",
        )

    # ── Private scoring helpers ───────────────────────────────────────────────

    def _liquidity_score(self, revenue: Decimal, expenses: Decimal) -> float:
        if revenue <= 0:
            return 0.0
        ratio = float(revenue / expenses) if expenses > 0 else 10.0
        return min(100.0, ratio / 3.0 * 100)

    def _profitability_score(self, profit: Decimal, revenue: Decimal) -> float:
        if revenue <= 0:
            return 0.0
        margin = float(profit / revenue)
        return max(0.0, min(100.0, margin * 300))

    def _cost_control_score(self, expense_ratio: Decimal) -> float:
        ratio = float(expense_ratio)
        if ratio >= 1.0:
            return 0.0
        return max(0.0, (1.0 - ratio) * 150)

    def _momentum_score(self, avg_daily: Decimal) -> float:
        # Basic: score based on daily sales threshold; extend with WoW comparison
        score = min(100.0, float(avg_daily) / 50_000 * 100)
        return max(0.0, score)

    def _debt_coverage_score(self, profit: Decimal, expenses: Decimal) -> float:
        if expenses <= 0:
            return 100.0
        coverage = float(profit / expenses)
        return max(0.0, min(100.0, coverage * 50))
