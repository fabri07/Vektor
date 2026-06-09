"""FASE 4: schema del resumen económico analítico."""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel


class EconomicSummaryResponse(BaseModel):
    """Resumen económico gerencial (analítico, NO contable) para un rango."""

    model_config = {"from_attributes": True}

    from_date: date
    to_date: date
    total_income_ars: float
    total_expenses_ars: float
    net_result_ars: float
    stock_value_ars: float
    # Productos activos sin unit_cost_ars: no suman al valor de stock; se reportan
    # para que el usuario complete los costos faltantes (no se inventa el costo).
    missing_cost_count: int
    missing_cost_stock_units: int
    # False cuando no hay movimientos en el período ni stock cargado → empty state.
    has_data: bool
