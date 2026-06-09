"""FASE 4: resumen económico analítico (NO contable).

Resumen gerencial para un rango de fechas: ingresos, egresos, resultado y stock
valorizado. Reusa los repos existentes para ventas/gastos (ya filtran soft-delete
y usan func.date sobre transaction_date DATETIME) y agrega la valuación de stock.

NO es contabilidad: no calcula activos/pasivos/patrimonio ni estados contables.
Los productos sin `unit_cost_ars` NO suman al valor de stock y se reportan aparte
(missing_cost_*) — nunca se inventa un costo.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import and_, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.models.product import Product
from app.persistence.repositories.transaction_repository import (
    ExpenseRepository,
    SaleRepository,
)


async def _stock_valuation(
    session: AsyncSession, tenant_id: uuid.UUID
) -> tuple[float, int, int]:
    """Valuación de stock = Σ stock_units × unit_cost_ars (solo productos activos).

    Devuelve (stock_value_ars, missing_cost_count, missing_cost_stock_units).
    Productos con `unit_cost_ars` NULL no suman al valor (no se inventa el costo);
    se cuentan aparte SOLO si tienen `stock_units > 0` (un producto sin costo y sin
    stock no es "dato cargado" valuable → no debe activar has_data ni missing_cost).
    """
    _missing = and_(Product.unit_cost_ars.is_(None), Product.stock_units > 0)
    stmt = select(
        func.coalesce(func.sum(Product.stock_units * Product.unit_cost_ars), 0),
        func.coalesce(func.sum(case((_missing, 1), else_=0)), 0),
        func.coalesce(func.sum(case((_missing, Product.stock_units), else_=0)), 0),
    ).where(
        Product.tenant_id == tenant_id,
        Product.is_active.is_(True),
    )
    row = (await session.execute(stmt)).one()
    stock_value = float(row[0] or Decimal("0"))
    missing_cost_count = int(row[1] or 0)
    missing_cost_stock_units = int(row[2] or 0)
    return stock_value, missing_cost_count, missing_cost_stock_units


async def compute_economic_summary(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    from_date: date,
    to_date: date,
) -> dict[str, object]:
    """Calcula el resumen económico del período para un tenant.

    `net_result = total_income - total_expenses`. `has_data=False` cuando no hay
    ni movimientos en el período ni stock cargado → el frontend muestra empty state.
    """
    sale_repo = SaleRepository(session)
    expense_repo = ExpenseRepository(session)

    total_income = await sale_repo.total_revenue(tenant_id, from_date, to_date)
    total_expenses = await expense_repo.total_expenses(tenant_id, from_date, to_date)
    net_result = total_income - total_expenses

    stock_value, missing_cost_count, missing_cost_stock_units = await _stock_valuation(
        session, tenant_id
    )

    has_data = bool(
        total_income or total_expenses or stock_value or missing_cost_count
    )

    return {
        "from_date": from_date,
        "to_date": to_date,
        "total_income_ars": round(total_income, 2),
        "total_expenses_ars": round(total_expenses, 2),
        "net_result_ars": round(net_result, 2),
        "stock_value_ars": round(stock_value, 2),
        "missing_cost_count": missing_cost_count,
        "missing_cost_stock_units": missing_cost_stock_units,
        "has_data": has_data,
    }
