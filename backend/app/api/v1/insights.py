"""Insights and action suggestions endpoints."""

from datetime import date, datetime, timedelta
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import get_current_tenant
from app.persistence.db.session import get_db_session
from app.persistence.models.business import ActionSuggestion, Insight
from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import SaleEntry
from app.persistence.repositories.transaction_repository import ExpenseRepository

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
    low_stock_threshold_units: int
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

    # Productos con stock bajo o crítico
    products_result = await session.execute(
        select(Product).where(
            Product.tenant_id == tenant.tenant_id,
            Product.is_active.is_(True),
        )
    )
    all_products = list(products_result.scalars().all())
    total_products = len(all_products)
    low_stock = [
        p for p in all_products
        if p.stock_units is not None
        and p.low_stock_threshold_units is not None
        and p.stock_units <= p.low_stock_threshold_units
    ]
    sold_products_result = await session.execute(
        select(SaleEntry.product_id).where(
            SaleEntry.tenant_id == tenant.tenant_id,
            SaleEntry.transaction_date >= from_date,
            SaleEntry.transaction_date <= today,
            SaleEntry.product_id.isnot(None),
        ).distinct()
    )
    sold_product_ids = set(sold_products_result.scalars().all())
    no_rotation = [
        p
        for p in all_products
        if p.id not in sold_product_ids
        and (p.stock_units or 0) > (p.low_stock_threshold_units or 0)
    ]

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
                low_stock_threshold_units=p.low_stock_threshold_units or 0,
                sale_price_ars=float(p.sale_price_ars or 0),
            )
            for p in low_stock[:10]
        ],
        no_rotation_products=[
            ProductStockItem(
                product_id=str(p.id),
                name=p.name,
                stock_units=p.stock_units or 0,
                low_stock_threshold_units=p.low_stock_threshold_units or 0,
                sale_price_ars=float(p.sale_price_ars or 0),
            )
            for p in no_rotation[:10]
        ],
        low_stock_count=len(low_stock),
        no_rotation_count=len(no_rotation),
        total_products=total_products,
    )
