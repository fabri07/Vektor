"""FactsProvider — el seam entre FactsService y el ORM real de Véktor.

Este es el ÚNICO módulo donde los nombres de campo reales (SaleEntry.amount,
ExpenseEntry.expense_type, Product.unit_cost_ars...) se mapean al contrato
canónico de columnas que exige `FactsDataProvider` (facts_service.py).
FactsService no conoce SQLAlchemy; acá no se computa NINGUNA métrica.

Patrón: "preloaded". FactsService es sync puro y computa N facts por snapshot;
los repos son async. En lugar de async-ificar el servicio, `build_facts_service`
corre las queries UNA vez (cubriendo período actual + anterior para la
variación %) y arma un `PreloadedFactsProvider` sync que sirve los DataFrames
desde memoria. Mismo patrón fetch-then-compute que forecast_service.

Mapeos clave (contratos verificados en app/persistence/models/transaction.py):
    - cost_ars por venta NO existe como columna → se deriva
      Product.unit_cost_ars × SaleEntry.quantity vía outerjoin (NaN si falta).
    - last_sold_date NO existe en Product → max(transaction_date) por product_id
      (histórico completo, sin ventana: el sobrestock necesita el máximo real).
    - soft-delete: voided_at IS NULL siempre (deactivated products fuera).
    - provenance llega crudo (REAL/DEMO); el filtro lo hace FactsService.
    - payment_method="inflow" (cobros de fiado) SE INCLUYE: FactsService decide.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime

import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.facts_service import (
    FactsService,
    Period,
    SeverityThresholds,
)
from app.domain.business_time import AR_TZ
from app.domain.verticals import Vertical
from app.persistence.models.product import Product
from app.persistence.models.transaction import ExpenseEntry, SaleEntry


def _naive_ar(value: datetime | None) -> datetime | None:
    """Normaliza a datetime naive en hora AR (created_at es tz-aware UTC;
    acquired_at/transaction_date son naive — mezclar rompe pd.to_datetime)."""
    if value is None or value.tzinfo is None:
        return value
    return value.astimezone(AR_TZ).replace(tzinfo=None)

_SALES_COLS = ["sale_date", "amount_ars", "cost_ars", "payment_method", "provenance"]
_EXPENSES_COLS = ["expense_date", "amount_ars", "category", "expense_type", "provenance"]
_PRODUCTS_COLS = [
    "product_id", "stock_units", "unit_cost_ars", "last_sold_date",
    "first_seen_date", "provenance",
]


@dataclass(frozen=True)
class FactsFrames:
    """DataFrames normalizados + tenant y rango que cubren (guards anti mal uso)."""

    tenant_id: str
    sales: pd.DataFrame
    expenses: pd.DataFrame
    products: pd.DataFrame
    coverage_start: date
    coverage_end: date


async def load_facts_frames(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    start: date,
    end: date,
) -> FactsFrames:
    """Corre las queries bulk (sin limit) y arma los DataFrames canónicos."""
    # ── Ventas + costo derivado (outerjoin: product_id es nullable) ──────────
    sales_q = (
        select(
            SaleEntry.transaction_date,
            SaleEntry.amount,
            SaleEntry.quantity,
            SaleEntry.payment_method,
            SaleEntry.provenance,
            Product.unit_cost_ars,
        )
        .outerjoin(Product, Product.id == SaleEntry.product_id)
        .where(
            SaleEntry.tenant_id == tenant_id,
            SaleEntry.voided_at.is_(None),
            func.date(SaleEntry.transaction_date) >= start,
            func.date(SaleEntry.transaction_date) <= end,
        )
    )
    sales_rows = (await session.execute(sales_q)).all()
    sales_df = pd.DataFrame(
        [
            {
                "sale_date": row.transaction_date,
                "amount_ars": float(row.amount),
                "cost_ars": (
                    float(row.unit_cost_ars) * row.quantity
                    if row.unit_cost_ars is not None
                    else float("nan")
                ),
                "payment_method": row.payment_method,
                "provenance": row.provenance,
            }
            for row in sales_rows
        ],
        columns=_SALES_COLS,
    )

    # ── Gastos (expense_type = frontera canónica COGS/OPEX) ─────────────────
    expenses_q = select(
        ExpenseEntry.transaction_date,
        ExpenseEntry.amount,
        ExpenseEntry.category,
        ExpenseEntry.expense_type,
        ExpenseEntry.provenance,
    ).where(
        ExpenseEntry.tenant_id == tenant_id,
        ExpenseEntry.voided_at.is_(None),
        func.date(ExpenseEntry.transaction_date) >= start,
        func.date(ExpenseEntry.transaction_date) <= end,
    )
    expense_rows = (await session.execute(expenses_q)).all()
    expenses_df = pd.DataFrame(
        [
            {
                "expense_date": row.transaction_date,
                "amount_ars": float(row.amount),
                "category": row.category,
                "expense_type": row.expense_type,
                "provenance": row.provenance,
            }
            for row in expense_rows
        ],
        columns=_EXPENSES_COLS,
    )

    # ── Productos activos + last_sold_date derivado (histórico completo) ────
    products_q = select(
        Product.id,
        Product.stock_units,
        Product.unit_cost_ars,
        Product.acquired_at,
        Product.created_at,
        Product.provenance,
    ).where(
        Product.tenant_id == tenant_id,
        Product.is_active.is_(True),
        Product.deactivated_at.is_(None),
    )
    product_rows = (await session.execute(products_q)).all()

    last_sold_q = (
        select(
            SaleEntry.product_id,
            func.max(SaleEntry.transaction_date).label("last_sold"),
        )
        .where(
            SaleEntry.tenant_id == tenant_id,
            SaleEntry.voided_at.is_(None),
            SaleEntry.product_id.isnot(None),
        )
        .group_by(SaleEntry.product_id)
    )
    last_sold_rows = (await session.execute(last_sold_q)).all()
    last_sold_map = {str(row.product_id): row.last_sold for row in last_sold_rows}

    products_df = pd.DataFrame(
        [
            {
                "product_id": str(row.id),
                "stock_units": float(row.stock_units),
                "unit_cost_ars": (
                    float(row.unit_cost_ars) if row.unit_cost_ars is not None
                    else float("nan")
                ),
                "last_sold_date": last_sold_map.get(str(row.id), pd.NaT),
                # ancla de antigüedad para sobrestock de nunca-vendidos
                "first_seen_date": _naive_ar(row.acquired_at or row.created_at),
                "provenance": row.provenance,
            }
            for row in product_rows
        ],
        columns=_PRODUCTS_COLS,
    )

    return FactsFrames(
        tenant_id=str(tenant_id),
        sales=sales_df,
        expenses=expenses_df,
        products=products_df,
        coverage_start=start,
        coverage_end=end,
    )


class PreloadedFactsProvider:
    """Implementa el Protocol `FactsDataProvider` (sync) sirviendo frames precargados.

    Dos guards, misma filosofía que la no-invention rule (fallar explícito antes
    que responder con data equivocada en silencio):
    - rango fuera del precargado → ValueError;
    - tenant distinto al de los frames → ValueError (un provider precargado es
      de UN tenant; servir los números de otro sería una fuga silenciosa).
    """

    def __init__(self, frames: FactsFrames) -> None:
        self._frames = frames

    def _check_tenant(self, tenant_id: str) -> None:
        if str(tenant_id) != self._frames.tenant_id:
            raise ValueError(
                f"Provider precargado para tenant {self._frames.tenant_id}; "
                f"se pidió {tenant_id}"
            )

    def _check_coverage(self, start: date, end: date) -> None:
        if start < self._frames.coverage_start or end > self._frames.coverage_end:
            raise ValueError(
                f"Rango [{start}, {end}] fuera de la cobertura precargada "
                f"[{self._frames.coverage_start}, {self._frames.coverage_end}]"
            )

    @staticmethod
    def _slice(df: pd.DataFrame, date_col: str, start: date, end: date) -> pd.DataFrame:
        if df.empty:
            return df.copy()
        d = pd.to_datetime(df[date_col]).dt.date
        return df[(d >= start) & (d <= end)].copy()

    def sales(self, tenant_id: str, start: date, end: date) -> pd.DataFrame:
        self._check_tenant(tenant_id)
        self._check_coverage(start, end)
        return self._slice(self._frames.sales, "sale_date", start, end)

    def expenses(self, tenant_id: str, start: date, end: date) -> pd.DataFrame:
        self._check_tenant(tenant_id)
        self._check_coverage(start, end)
        return self._slice(self._frames.expenses, "expense_date", start, end)

    def products(self, tenant_id: str) -> pd.DataFrame:
        self._check_tenant(tenant_id)
        return self._frames.products.copy()


def thresholds_for_vertical(vertical: Vertical) -> SeverityThresholds:
    """Umbrales de severidad desde el benchmark canónico del vertical.

    MISMA fuente JSON que usa el health engine (heuristics/verticals/loader.py),
    convertida de fracciones a % — así el severity del fact y el health score
    no pueden contradecirse. Sin fallback: el vertical llega tipado.
    """
    from app.heuristics.verticals.loader import load_margin_benchmark  # noqa: PLC0415

    margin = load_margin_benchmark(vertical)
    return SeverityThresholds(
        margen_neto_floor=float(margin.warning_below) * 100.0,
        margen_neto_critical=float(margin.critical_below) * 100.0,
    )


async def build_facts_service(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    period: Period,
    *,
    vertical: Vertical | None = None,
    thresholds: SeverityThresholds | None = None,
) -> FactsService:
    """Carga [período_anterior.start, período.end] en UNA pasada y arma el servicio.

    La cobertura incluye el período previo porque ventas_periodo/ticket_promedio
    computan la variación % contra él. Si se pasa `vertical`, los umbrales de
    severidad salen del benchmark JSON del vertical (`thresholds` explícito tiene
    precedencia). Con ambos en `None` el servicio queda sin umbrales de margen
    —y `margen_neto` sin severity— en vez de heredar los de un rubro ajeno.
    """
    if thresholds is None and vertical is not None:
        thresholds = thresholds_for_vertical(vertical)
    frames = await load_facts_frames(
        session,
        tenant_id,
        start=period.previous().start,
        end=period.end,
    )
    return FactsService(PreloadedFactsProvider(frames), thresholds)
