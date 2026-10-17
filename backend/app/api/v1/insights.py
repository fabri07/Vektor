"""Insights and action suggestions endpoints."""

from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import get_current_tenant, require_active_subscription_for_insights
from app.domain.product import DEFAULT_LOW_STOCK_THRESHOLD_UNITS
from app.domain.sale_unit import formatear_cantidad, valor_de_stock
from app.persistence.db.session import get_db_session
from app.persistence.models.business import ActionSuggestion, Insight
from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import SaleEntry
from app.persistence.repositories.inventory_repository import InventoryRepository
from app.persistence.repositories.transaction_repository import ExpenseRepository, SaleRepository

router = APIRouter()

# B14: las lecturas son análisis y llevan el gate; el PATCH de acknowledge es
# una corrección y no.
_GATE_DE_ANALISIS = [Depends(require_active_subscription_for_insights)]


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
    dependencies=_GATE_DE_ANALISIS,
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


@router.get(
    "",
    response_model=list[InsightResponse],
    summary="List insights",
    dependencies=_GATE_DE_ANALISIS,
)
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
    dependencies=_GATE_DE_ANALISIS,
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


class ProductStockItem(BaseModel):
    product_id: str
    name: str
    stock_units: int
    # None = umbral no configurado; el frontend muestra el default (5) con ?? 5
    low_stock_threshold_units: int | None
    sale_price_ars: float
    # Plata inmovilizada en este producto: stock × costo / factor (0 si no hay costo).
    immobilized_value: float = 0.0
    # El stock está en unidades base: sin la unidad, 5000 de un producto en
    # gramos se leería como 5000 unidades.
    sale_unit: str = "unit"
    base_units_per_sale_unit: int = 1
    stock_display: str | None = None


class SupplierBreakdownProductItem(BaseModel):
    product_id: str
    name: str
    brand: str
    total_amount: float
    total_qty: float


class SupplierPurchaseBreakdownItem(BaseModel):
    supplier_id: str | None
    supplier_name: str
    is_unassigned: bool
    total_purchased: float
    coverage_pct: float
    products: list[SupplierBreakdownProductItem]


class BusinessBreakdownResponse(BaseModel):
    period_days: int
    from_date: str
    to_date: str
    expenses_by_category: list[CategoryBreakdownItem]
    # Desglose de compras de mercadería por proveedor REAL (acordeón del dashboard).
    suppliers_breakdown: list[SupplierPurchaseBreakdownItem]
    low_stock_products: list[ProductStockItem]
    no_rotation_products: list[ProductStockItem]
    low_stock_count: int
    no_rotation_count: int
    total_products: int
    # Plata parada en productos sin rotación (todos, no solo los listados) + cuántos
    # no se pudieron valuar por falta de costo unitario.
    no_rotation_value_total: float = 0.0
    no_rotation_unvalued_count: int = 0
    # FASE D: desglose contable del período — OPEX (operativo) vs COGS
    # (compras de mercadería) + valor de stock actual (stock × costo unitario).
    opex_total: float = 0.0
    cogs_total: float = 0.0
    stock_value_total: float = 0.0


@router.get(
    "/breakdown",
    response_model=BusinessBreakdownResponse,
    summary="Desglose de gastos, proveedores y stock del período",
    dependencies=_GATE_DE_ANALISIS,
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
    # FASE D: balance mercadería — salida de caja (COGS) vs valor del stock.
    type_totals = await expense_repo.totals_by_expense_type(
        tenant.tenant_id, from_date=from_date, to_date=today
    )
    stock_value_result = await session.execute(
        select(
            func.coalesce(
                # Costo por unidad de VENTA × stock en unidades base: divide por
                # el factor (5000 g a $800/kg valen $4.000, no $4.000.000).
                func.sum(
                    func.coalesce(Product.stock_units, 0)
                    * func.coalesce(Product.unit_cost_ars, 0)
                    / Product.base_units_per_sale_unit
                ),
                0,
            )
        ).where(
            Product.tenant_id == tenant.tenant_id,
            Product.is_active.is_(True),
        )
    )
    stock_value_total = float(stock_value_result.scalar_one() or 0)

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

    # Sin rotación (definición corregida): producto activo + stock > 0 + ninguna
    # venta NO anulada en el período. Ya NO exige superar el umbral mínimo — eso
    # excluía productos con stock bajo pero positivo que tampoco rotaron.
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
        func.coalesce(Product.stock_units, 0) > 0,
        ~_sold_subq,
    ]
    no_rotation_count_result = await session.execute(
        select(func.count(Product.id)).where(*_no_rotation_where)
    )
    no_rotation_count_val = no_rotation_count_result.scalar_one()
    # Plata parada total (todos los sin-rotación, no solo los listados) + cuántos
    # no se pueden valuar por unit_cost_ars NULL.
    # Costo por unidad de venta, stock en unidades base: se divide por el factor.
    _immobilized_expr = (
        func.coalesce(Product.stock_units, 0)
        * func.coalesce(Product.unit_cost_ars, 0)
        / Product.base_units_per_sale_unit
    )
    no_rotation_value_total = float(
        (
            await session.execute(
                select(func.coalesce(func.sum(_immobilized_expr), 0)).where(*_no_rotation_where)
            )
        ).scalar_one()
        or 0
    )
    no_rotation_unvalued_count = (
        await session.execute(
            select(func.count(Product.id)).where(
                *_no_rotation_where, Product.unit_cost_ars.is_(None)
            )
        )
    ).scalar_one()
    no_rotation_items_result = await session.execute(
        select(Product).where(*_no_rotation_where).order_by(_immobilized_expr.desc()).limit(10)
    )
    no_rotation = list(no_rotation_items_result.scalars().all())

    # Desglose de compras por proveedor real (reemplaza la agrupación por texto libre).
    # HISTÓRICO (sin filtro de fecha): inventory_movements solo tiene created_at (= día
    # de import, no fecha de compra), así que filtrar por período mezclaría semánticas
    # con los demás paneles (que usan transaction_date). La relación con un proveedor es
    # de largo plazo; mostramos el acumulado y el panel lo rotula como tal.
    suppliers_breakdown = await InventoryRepository(session).suppliers_purchase_breakdown(
        tenant.tenant_id
    )

    def _stock_item(p: Product) -> ProductStockItem:
        stock = p.stock_units or 0
        cost = float(p.unit_cost_ars or 0)
        return ProductStockItem(
            product_id=str(p.id),
            name=p.name,
            stock_units=stock,
            low_stock_threshold_units=p.low_stock_threshold_units,
            sale_price_ars=float(p.sale_price_ars or 0),
            sale_unit=p.sale_unit or "unit",
            base_units_per_sale_unit=p.base_units_per_sale_unit or 1,
            stock_display=formatear_cantidad(
                stock, p.sale_unit or "unit", p.base_units_per_sale_unit or 1
            ),
            immobilized_value=float(
                valor_de_stock(stock, p.base_units_per_sale_unit or 1, Decimal(str(cost)))
            ),
        )

    return BusinessBreakdownResponse(
        period_days=days,
        from_date=from_date.isoformat(),
        to_date=today.isoformat(),
        opex_total=type_totals["OPEX"],
        cogs_total=type_totals["COGS"],
        stock_value_total=stock_value_total,
        expenses_by_category=[CategoryBreakdownItem(**item) for item in expenses_by_cat],
        suppliers_breakdown=[
            SupplierPurchaseBreakdownItem(
                supplier_id=str(s.supplier_id) if s.supplier_id else None,
                supplier_name=s.supplier_name,
                is_unassigned=s.is_unassigned,
                total_purchased=s.total_purchased,
                coverage_pct=s.coverage_pct,
                products=[
                    SupplierBreakdownProductItem(
                        product_id=str(pr.product_id),
                        name=pr.name,
                        brand=pr.brand,
                        total_amount=pr.total_amount,
                        total_qty=pr.total_qty,
                    )
                    for pr in s.products
                ],
            )
            for s in suppliers_breakdown
        ],
        low_stock_products=[_stock_item(p) for p in low_stock[:10]],
        no_rotation_products=[_stock_item(p) for p in no_rotation[:10]],
        low_stock_count=low_stock_count_val,
        no_rotation_count=no_rotation_count_val,
        total_products=total_products,
        no_rotation_value_total=no_rotation_value_total,
        no_rotation_unvalued_count=no_rotation_unvalued_count,
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
    rows: list[dict[str, Any]], sorted_dates: list[str]
) -> dict[str, list[float]]:
    """Transforma filas (date, payment_method, total) en series alineadas con sorted_dates."""
    method_set = sorted({r["payment_method"] for r in rows})
    lookup: dict[tuple[str, str], float] = {
        (r["date"], r["payment_method"]): r["total"] for r in rows
    }
    return {method: [lookup.get((d, method), 0.0) for d in sorted_dates] for method in method_set}


def _to_week_start(date_str: str) -> str:
    """Retorna la fecha del lunes de la semana del date_str dado."""
    d = date.fromisoformat(date_str)
    return (d - timedelta(days=d.weekday())).isoformat()


def _aggregate_weekly(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Consolida filas diarias en filas semanales (date = lunes de la semana)."""
    acc: dict[tuple[str, str], float] = {}
    for r in rows:
        key = (_to_week_start(r["date"]), r["payment_method"])
        acc[key] = acc.get(key, 0.0) + r["total"]
    return [{"date": k[0], "payment_method": k[1], "total": v} for k, v in sorted(acc.items())]


@router.get(
    "/cash-breakdown",
    response_model=CashBreakdownResponse,
    summary="Ingresos y egresos por método de pago en el período",
    dependencies=_GATE_DE_ANALISIS,
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


# ── M·3-2: el margen en pantalla (FactsService, sin fórmula en el front) ──────


class MarginFactOut(BaseModel):
    """Un margen de FactsService con su estado: nunca un 0 inventado."""

    value: float | None
    #: confirmado | estimado | ausente
    estado: str
    motivo: str | None = None
    periodo: str | None = None
    desde: date | None = None
    hasta: date | None = None
    cobertura_confirmada_pct: float | None = None
    cobertura_total_pct: float | None = None
    ingreso_ars: float | None = None
    costo_vendido_ars: float | None = None
    #: Sólo en el neto.
    gastos_operativos_ars: float | None = None
    resultado_ars: float | None = None
    #: Sólo en el desglose por categoría.
    categoria: str | None = None
    #: M·3-4, sólo en el bruto: lo vendido con un costo de hace > 45 días (a la
    #: fecha de cada venta) y lo vendido con costo de antigüedad desconocida.
    costo_desactualizado_ars: float | None = None
    costo_desactualizado_pct: float | None = None
    costo_antiguedad_desconocida_ars: float | None = None
    costo_antiguedad_desconocida_pct: float | None = None
    #: El umbral (``COSTO_DESACTUALIZADO_DIAS``): el front no lo repite.
    costo_desactualizado_umbral_dias: int | None = None


class MarginsResponse(BaseModel):
    #: La completitud de los datos no llega a 50: estado vacío (no-invention).
    sin_datos: bool = False
    motivo: str | None = None
    neto_mes_cerrado: MarginFactOut | None = None
    bruto_mes_en_curso: MarginFactOut | None = None
    bruto_periodo: MarginFactOut | None = None
    #: dia | semana | mes (según el largo del período)
    granularidad: str = "dia"
    serie_bruto: list[MarginFactOut] = []
    #: Un punto por mes CERRADO del período (el neto es de mes cerrado).
    serie_neto: list[MarginFactOut] = []
    por_categoria: list[MarginFactOut] = []


def _num(v: object) -> float | None:
    return None if v is None else float(str(v))


def _margin_out(f: Any, desde: date, hasta: date) -> MarginFactOut:
    d = f.detail or {}
    return MarginFactOut(
        value=f.value,
        estado=str(d.get("estado") or ("ausente" if f.value is None else "confirmado")),
        motivo=d.get("motivo"),
        periodo=f.period,
        desde=desde,
        hasta=hasta,
        cobertura_confirmada_pct=_num(d.get("cobertura_confirmada_pct")),
        cobertura_total_pct=_num(d.get("cobertura_total_pct")),
        ingreso_ars=_num(d.get("ingreso_ars")),
        costo_vendido_ars=_num(d.get("costo_vendido_ars")),
        gastos_operativos_ars=_num(d.get("gastos_operativos_ars")),
        resultado_ars=_num(d.get("resultado_ars")),
        categoria=d.get("categoria"),
        costo_desactualizado_ars=_num(d.get("costo_desactualizado_ars")),
        costo_desactualizado_pct=_num(d.get("costo_desactualizado_pct")),
        costo_antiguedad_desconocida_ars=_num(d.get("costo_antiguedad_desconocida_ars")),
        costo_antiguedad_desconocida_pct=_num(d.get("costo_antiguedad_desconocida_pct")),
        costo_desactualizado_umbral_dias=(
            int(str(d["costo_desactualizado_umbral_dias"]))
            if d.get("costo_desactualizado_umbral_dias") is not None else None
        ),
    )


@router.get(
    "/margins",
    response_model=MarginsResponse,
    summary="Margen neto del mes cerrado, bruto en curso, series y desglose (M·3-2)",
    dependencies=_GATE_DE_ANALISIS,
)
async def get_margins(
    from_date: date | None = Query(default=None, description="Inicio del período (inclusive)"),
    to_date: date | None = Query(default=None, description="Fin del período (inclusive)"),
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db_session),
) -> MarginsResponse:
    """Todo sale de FactsService (invariante 2c): el front no calcula ningún margen.

    Sin período, el mes en curso. El neto es siempre del último mes cerrado; el
    período elige el bruto, la serie y el desglose por categoría.
    """
    from app.application.services.facts_provider import (  # noqa: PLC0415
        MOTIVO_SIN_RUBRO,
        margenes_para_pantalla,
    )
    from app.domain.business_time import today_ar  # noqa: PLC0415

    hoy = today_ar()
    hasta = min(to_date or hoy, hoy)
    desde = from_date or hasta.replace(day=1)
    if desde > hasta:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="from_date no puede ser posterior a to_date",
        )
    if (hasta - desde).days > 3 * 366:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="El período no puede superar los 3 años",
        )
    m = await margenes_para_pantalla(session, tenant.tenant_id, desde=desde, hasta=hasta)
    if m is None:
        return MarginsResponse(sin_datos=True, motivo=MOTIVO_SIN_RUBRO)
    if m.sin_datos:
        return MarginsResponse(
            sin_datos=True, motivo="faltan datos del negocio para medir el margen"
        )

    def out(f: Any) -> MarginFactOut:
        d = f.detail or {}
        desde_f = d.get("periodo_desde")
        hasta_f = d.get("periodo_hasta")
        return _margin_out(
            f,
            date.fromisoformat(str(desde_f)) if desde_f else desde,
            date.fromisoformat(str(hasta_f)) if hasta_f else hasta,
        )

    return MarginsResponse(
        neto_mes_cerrado=out(m.neto_mes_cerrado) if m.neto_mes_cerrado else None,
        bruto_mes_en_curso=out(m.bruto_mes_en_curso) if m.bruto_mes_en_curso else None,
        bruto_periodo=out(m.bruto_periodo) if m.bruto_periodo else None,
        granularidad=m.granularidad,
        serie_bruto=[out(f) for f in m.serie_bruto],
        serie_neto=[out(f) for f in m.serie_neto],
        por_categoria=[out(f) for f in m.por_categoria],
    )
