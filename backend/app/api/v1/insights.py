"""Insights and action suggestions endpoints."""

from datetime import date, datetime, timedelta
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import get_current_tenant
from app.domain.product import DEFAULT_LOW_STOCK_THRESHOLD_UNITS
from app.persistence.db.session import get_db_session
from app.persistence.models.business import ActionSuggestion, Insight
from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import SaleEntry
from app.persistence.repositories.transaction_repository import ExpenseRepository, SaleRepository

router = APIRouter()


# ── Schemas ───────────────────────────────────────────────────────────────────


class InsightResponse(BaseModel):
    model_config = {"from_attributes": True}

    id: UUID
    title: str
    description: str
    insight_type: str
    severity_code: str
    heuristic_version: str
    created_at: datetime


class ActionSuggestionResponse(BaseModel):
    model_config = {"from_attributes": True}

    id: UUID
    title: str
    description: str
    action_type: str
    risk_level: str
    status: str
    created_at: datetime


class CurrentInsightResponse(BaseModel):
    insight: InsightResponse
    action_suggestion: ActionSuggestionResponse | None


# ── Endpoints ─────────────────────────────────────────────────────────────────


@router.get(
    "/current",
    response_model=CurrentInsightResponse,
    summary="Active insight + action suggestion for the current tenant",
)
async def get_current_insight(
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db_session),
) -> CurrentInsightResponse:
    """
    Returns the most recent Insight and its associated ActionSuggestion.
    Raises 404 if no insight has been generated yet.
    """
    insight_result = await session.execute(
        select(Insight)
        .where(Insight.tenant_id == tenant.tenant_id)
        .order_by(Insight.created_at.desc())
        .limit(1)
    )
    insight = insight_result.scalar_one_or_none()

    if insight is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No insight available yet.",
        )

    action_result = await session.execute(
        select(ActionSuggestion)
        .where(
            ActionSuggestion.tenant_id == tenant.tenant_id,
            ActionSuggestion.insight_id == insight.id,
        )
        .order_by(ActionSuggestion.created_at.desc())
        .limit(1)
    )
    action = action_result.scalar_one_or_none()

    return CurrentInsightResponse(
        insight=InsightResponse.model_validate(insight),
        action_suggestion=ActionSuggestionResponse.model_validate(action) if action else None,
    )


@router.patch(
    "/actions/{action_id}/acknowledge",
    response_model=ActionSuggestionResponse,
    summary="Mark an action suggestion as acknowledged",
)
async def acknowledge_action(
    action_id: UUID,
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db_session),
) -> ActionSuggestionResponse:
    result = await session.execute(
        select(ActionSuggestion).where(
            ActionSuggestion.id == action_id,
            ActionSuggestion.tenant_id == tenant.tenant_id,
        )
    )
    action = result.scalar_one_or_none()
    if action is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Action suggestion not found.",
        )
    action.status = "acknowledged"
    await session.commit()
    await session.refresh(action)
    return ActionSuggestionResponse.model_validate(action)


@router.get("", response_model=list[InsightResponse], summary="List insights")
async def list_insights(
    limit: int = Query(default=20, ge=1, le=100),
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db_session),
) -> list[InsightResponse]:
    q = (
        select(Insight)
        .where(Insight.tenant_id == tenant.tenant_id)
        .order_by(Insight.created_at.desc())
        .limit(limit)
    )
    result = await session.execute(q)
    return [InsightResponse.model_validate(i) for i in result.scalars().all()]


@router.get(
    "/actions",
    response_model=list[ActionSuggestionResponse],
    summary="List action suggestions",
)
async def list_actions(
    status_filter: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=20, ge=1, le=100),
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db_session),
) -> list[ActionSuggestionResponse]:
    q = select(ActionSuggestion).where(ActionSuggestion.tenant_id == tenant.tenant_id)
    if status_filter:
        q = q.where(ActionSuggestion.status == status_filter)
    q = q.order_by(ActionSuggestion.created_at.desc()).limit(limit)
    result = await session.execute(q)
    return [ActionSuggestionResponse.model_validate(a) for a in result.scalars().all()]


# ── Breakdown endpoint ────────────────────────────────────────────────────────


class CategoryBreakdownItem(BaseModel):
    category: str
    total: float
    pct: float


class SupplierBreakdownItem(BaseModel):
    supplier_name: str
    total: float
    pct: float


class ProductStockItem(BaseModel):
    product_id: str
    name: str
    stock_units: int
    # None = umbral no configurado; el frontend muestra el default (5) con ?? 5
    low_stock_threshold_units: int | None
    sale_price_ars: float


class BusinessBreakdownResponse(BaseModel):
    period_days: int
    from_date: str
    to_date: str
    expenses_by_category: list[CategoryBreakdownItem]
    top_suppliers: list[SupplierBreakdownItem]
    low_stock_products: list[ProductStockItem]
    no_rotation_products: list[ProductStockItem]
    low_stock_count: int
    no_rotation_count: int
    total_products: int


@router.get(
    "/breakdown",
    response_model=BusinessBreakdownResponse,
    summary="Desglose de gastos, proveedores y stock del período",
)
async def get_business_breakdown(
    days: int = Query(default=30, ge=7, le=365, description="Ventana en días hacia atrás"),
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db_session),
) -> BusinessBreakdownResponse:
    today = date.today()
    from_date = today - timedelta(days=days)

    expense_repo = ExpenseRepository(session)

    expenses_by_cat = await expense_repo.expenses_by_category(
        tenant.tenant_id, from_date=from_date, to_date=today
    )
    top_suppliers = await expense_repo.top_suppliers(
        tenant.tenant_id, from_date=from_date, to_date=today, limit=5
    )

    # Total de productos activos (COUNT, sin cargar registros)
    total_result = await session.execute(
        select(func.count(Product.id)).where(
            Product.tenant_id == tenant.tenant_id,
            Product.is_active.is_(True),
        )
    )
    total_products = total_result.scalar_one()

    # Stock bajo: usa COALESCE para incluir productos con threshold NULL (default=5)
    _effective_threshold = func.coalesce(
        Product.low_stock_threshold_units, DEFAULT_LOW_STOCK_THRESHOLD_UNITS
    )
    _low_stock_where = [
        Product.tenant_id == tenant.tenant_id,
        Product.is_active.is_(True),
        Product.stock_units.isnot(None),
        Product.stock_units <= _effective_threshold,
    ]
    low_stock_count_result = await session.execute(
        select(func.count(Product.id)).where(*_low_stock_where)
    )
    low_stock_count_val = low_stock_count_result.scalar_one()
    low_stock_items_result = await session.execute(
        select(Product).where(*_low_stock_where).limit(10)
    )
    low_stock = list(low_stock_items_result.scalars().all())

    # Sin rotación: NOT EXISTS correlacionado (sin traer IDs a Python)
    _sold_subq = (
        select(SaleEntry.product_id)
        .where(
            SaleEntry.tenant_id == tenant.tenant_id,
            SaleEntry.voided_at.is_(None),
            SaleEntry.transaction_date >= from_date,
            SaleEntry.transaction_date <= today,
            SaleEntry.product_id == Product.id,
        )
        .correlate(Product)
        .exists()
    )
    _no_rotation_where = [
        Product.tenant_id == tenant.tenant_id,
        Product.is_active.is_(True),
        func.coalesce(Product.stock_units, 0) > _effective_threshold,
        ~_sold_subq,
    ]
    no_rotation_count_result = await session.execute(
        select(func.count(Product.id)).where(*_no_rotation_where)
    )
    no_rotation_count_val = no_rotation_count_result.scalar_one()
    no_rotation_items_result = await session.execute(
        select(Product).where(*_no_rotation_where).limit(10)
    )
    no_rotation = list(no_rotation_items_result.scalars().all())

    return BusinessBreakdownResponse(
        period_days=days,
        from_date=from_date.isoformat(),
        to_date=today.isoformat(),
        expenses_by_category=[CategoryBreakdownItem(**item) for item in expenses_by_cat],
        top_suppliers=[SupplierBreakdownItem(**item) for item in top_suppliers],
        low_stock_products=[
            ProductStockItem(
                product_id=str(p.id),
                name=p.name,
                stock_units=p.stock_units or 0,
                low_stock_threshold_units=p.low_stock_threshold_units,
                sale_price_ars=float(p.sale_price_ars or 0),
            )
            for p in low_stock[:10]
        ],
        no_rotation_products=[
            ProductStockItem(
                product_id=str(p.id),
                name=p.name,
                stock_units=p.stock_units or 0,
                low_stock_threshold_units=p.low_stock_threshold_units,
                sale_price_ars=float(p.sale_price_ars or 0),
            )
            for p in no_rotation[:10]
        ],
        low_stock_count=low_stock_count_val,
        no_rotation_count=no_rotation_count_val,
        total_products=total_products,
    )


# ── Cash breakdown by payment method ─────────────────────────────────────────


class CashBreakdownResponse(BaseModel):
    days: int
    granularity: str
    from_date: str
    to_date: str
    dates: list[str]
    income_series: dict[str, list[float]]
    expense_series: dict[str, list[float]]


def _aggregate_to_series(
    rows: list[dict], sorted_dates: list[str]
) -> dict[str, list[float]]:
    """Transforma filas (date, payment_method, total) en series alineadas con sorted_dates."""
    method_set = sorted({r["payment_method"] for r in rows})
    lookup: dict[tuple[str, str], float] = {
        (r["date"], r["payment_method"]): r["total"] for r in rows
    }
    return {
        method: [lookup.get((d, method), 0.0) for d in sorted_dates]
        for method in method_set
    }


def _to_week_start(date_str: str) -> str:
    """Retorna la fecha del lunes de la semana del date_str dado."""
    d = date.fromisoformat(date_str)
    return (d - timedelta(days=d.weekday())).isoformat()


def _aggregate_weekly(rows: list[dict]) -> list[dict]:
    """Consolida filas diarias en filas semanales (date = lunes de la semana)."""
    acc: dict[tuple[str, str], float] = {}
    for r in rows:
        key = (_to_week_start(r["date"]), r["payment_method"])
        acc[key] = acc.get(key, 0.0) + r["total"]
    return [
        {"date": k[0], "payment_method": k[1], "total": v}
        for k, v in sorted(acc.items())
    ]


@router.get(
    "/cash-breakdown",
    response_model=CashBreakdownResponse,
    summary="Ingresos y egresos por método de pago en el período",
)
async def get_cash_breakdown(
    days: int = Query(default=30, ge=7, le=365, description="Ventana en días hacia atrás"),
    granularity: str = Query(default="daily", pattern="^(daily|weekly)$"),
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db_session),
) -> CashBreakdownResponse:
    today = date.today()
    from_date = today - timedelta(days=days)

    sale_repo = SaleRepository(session)
    expense_repo = ExpenseRepository(session)

    income_rows = await sale_repo.cash_breakdown_by_method(tenant.tenant_id, from_date, today)
    expense_rows = await expense_repo.cash_breakdown_by_method(tenant.tenant_id, from_date, today)

    if granularity == "weekly":
        income_rows = _aggregate_weekly(income_rows)
        expense_rows = _aggregate_weekly(expense_rows)

    all_dates = sorted({r["date"] for r in income_rows} | {r["date"] for r in expense_rows})

    return CashBreakdownResponse(
        days=days,
        granularity=granularity,
        from_date=from_date.isoformat(),
        to_date=today.isoformat(),
        dates=all_dates,
        income_series=_aggregate_to_series(income_rows, all_dates),
        expense_series=_aggregate_to_series(expense_rows, all_dates),
    )
