"""Motor estadístico/financiero determinístico para Véktor.

REGLA DE ORO: este módulo NUNCA importa anthropic ni llama LLM.
Calcula con numpy + numpy-financial y devuelve dicts puros.
El sub-narrador (LLM) convierte los dicts en lenguaje natural.

Stack permitido: numpy + numpy-financial ÚNICAMENTE.
scipy/statsmodels están diferidos intencionalmente (no instalar).

Guardas de tamaño de muestra (no-invention rule):
  - serie vacía o n < mínimo → {status: "insufficient_data", n, min_required}
  - división por cero → None + flag booleano; NUNCA inf/nan crudo
  - std == 0 → detect_anomalies devuelve []
  - NO `or FALLBACK` cuando 0 es valor válido

Diferencia vs forecast_service.ForecastService:
  - ForecastService proyecta FLUJO DE CAJA NETO (income - expense) con
    3 tiers EWMA/tendencia usando historial de snapshots de la base de datos.
  - project_sales (este módulo) proyecta VENTAS AISLADAS a partir de una
    lista de valores diarios ya cargados por el agente; es más ligero y
    orientado a análisis puntual (sin acceso a repo ni DB).
"""
from __future__ import annotations

import math
from typing import Any

import numpy as np
import numpy_financial as npf

# ── Estadística descriptiva ──────────────────────────────────────────────────


def describe_sales(values: list[float]) -> dict[str, Any]:
    """Estadística descriptiva de una serie de ventas.

    Args:
        values: lista de montos de venta (float).

    Returns:
        Dict con {mean, median, std, cv, p25, p75, min, max, n} o
        {status: "insufficient_data", n: 0} si la lista está vacía.

    Notas:
        - cv (coeficiente de variación) = std/mean. Si mean==0 → cv=None.
        - std usa ddof=0 (población); adecuado para describir la muestra completa.
    """
    n = len(values)
    if n == 0:
        return {"status": "insufficient_data", "n": 0, "min_required": 1}

    arr = np.array(values, dtype=np.float64)
    mean = float(np.mean(arr))
    std = float(np.std(arr, ddof=0))
    median = float(np.median(arr))
    p25 = float(np.percentile(arr, 25))
    p75 = float(np.percentile(arr, 75))
    minimum = float(np.min(arr))
    maximum = float(np.max(arr))

    mean_r = round(mean, 4)
    std_r = round(std, 4)

    # cv: calculado sobre valores redondeados para coherencia con result["std"]/result["mean"]
    # cv=None cuando mean==0 para evitar división por cero (no-invention rule)
    cv: float | None = None
    if mean_r != 0.0:
        cv = std_r / mean_r

    return {
        "n": n,
        "mean": mean_r,
        "median": round(median, 4),
        "std": std_r,
        "cv": cv,
        "p25": round(p25, 4),
        "p75": round(p75, 4),
        "min": round(minimum, 4),
        "max": round(maximum, 4),
    }


# ── Proyección de ventas ─────────────────────────────────────────────────────

_MIN_SAMPLE_PROJECT = 7


def project_sales(
    daily_values: list[float],
    days_ahead: int = 15,
) -> dict[str, Any]:
    """Proyecta ventas futuras con regresión lineal simple (numpy.polyfit, grado 1).

    Distinto de ForecastService: proyecta VENTAS AISLADAS a partir de datos
    ya cargados; no accede a la base de datos ni calcula flujo neto de caja.

    Args:
        daily_values: ventas diarias observadas (float, en orden temporal).
        days_ahead: cuántos días proyectar (default 15).

    Returns:
        {status, projection_total, projection_daily_avg, trend_slope,
         r_squared, days_ahead} si n >= 7, o
        {status: "insufficient_data", n, min_required: 7} si n < 7.
    """
    n = len(daily_values)
    if n < _MIN_SAMPLE_PROJECT:
        return {
            "status": "insufficient_data",
            "n": n,
            "min_required": _MIN_SAMPLE_PROJECT,
        }

    arr = np.array(daily_values, dtype=np.float64)
    x = np.arange(n, dtype=np.float64)

    # Regresión lineal: slope, intercept
    slope, intercept = np.polyfit(x, arr, 1)

    # r_squared manual con numpy (sin statsmodels)
    y_pred = slope * x + intercept
    ss_res = float(np.sum((arr - y_pred) ** 2))
    ss_tot = float(np.sum((arr - np.mean(arr)) ** 2))
    # r_squared=1.0 si la serie es perfectamente plana (ss_tot==0 → ajuste perfecto)
    r_squared = 1.0 if ss_tot == 0.0 else max(0.0, 1.0 - ss_res / ss_tot)

    # Proyección: puntos futuros (x = n, n+1, ..., n+days_ahead-1).
    # Clamp a 0: una tendencia en caída pronunciada extrapola a valores negativos,
    # pero "vender una cantidad negativa" no tiene sentido — se reporta 0 (no
    # inventamos ventas, pero tampoco ventas negativas).
    future_x = np.arange(n, n + days_ahead, dtype=np.float64)
    future_y = np.maximum(slope * future_x + intercept, 0.0)
    projection_total = float(np.sum(future_y))
    projection_daily_avg = projection_total / days_ahead if days_ahead > 0 else 0.0

    return {
        "status": "ok",
        "trend_slope": round(float(slope), 6),
        "projection_total": round(projection_total, 2),
        "projection_daily_avg": round(projection_daily_avg, 2),
        "r_squared": round(r_squared, 4),
        "days_ahead": days_ahead,
        "n": n,
    }


# ── Capital de trabajo ───────────────────────────────────────────────────────


def working_capital(
    avg_daily_sales: float,
    inventory_days: int,
    receivables_days: int,
    payables_days: int,
) -> dict[str, Any]:
    """Calcula el ciclo de conversión de efectivo (CCC) y capital de trabajo necesario.

    CCC = inventory_days + receivables_days - payables_days.
    Capital de trabajo = avg_daily_sales × CCC.

    Args:
        avg_daily_sales: ventas promedio diarias (float, > 0).
        inventory_days: días promedio de inventario.
        receivables_days: días promedio de cuentas a cobrar.
        payables_days: días promedio de cuentas a pagar.

    Returns:
        {cash_conversion_cycle_days, working_capital_needed, interpretation}
        Interpretation: "favorable" si CCC < 30, "ajustado" si >= 30.
        {status: "insufficient_data"} si avg_daily_sales <= 0.
    """
    if avg_daily_sales <= 0.0:
        return {"status": "insufficient_data", "reason": "avg_daily_sales debe ser > 0"}

    ccc = inventory_days + receivables_days - payables_days
    wc_needed = avg_daily_sales * ccc
    interpretation = "favorable" if ccc < 30 else "ajustado"

    return {
        "cash_conversion_cycle_days": ccc,
        "working_capital_needed": round(wc_needed, 2),
        "interpretation": interpretation,
    }


# ── Rentabilidad por producto ────────────────────────────────────────────────


def product_profitability(
    sales: float,
    cogs: float,
    units: int,
) -> dict[str, Any]:
    """Calcula rentabilidad puntual de un producto.

    Clasificación de margen bruto:
        alta  : gross_margin_pct > 35%
        media : gross_margin_pct > 15%
        baja  : gross_margin_pct <= 15%

    Args:
        sales: ingresos por ventas del producto.
        cogs: costo de mercadería vendida.
        units: unidades vendidas.

    Returns:
        {gross_margin_pct, margin_per_unit, classification} si sales > 0 y units > 0.
        gross_margin_pct=None + division_by_zero=True si sales==0.
        margin_per_unit=None + units_division_by_zero=True si units==0.
    """
    result: dict[str, Any] = {}

    # Margen bruto porcentual
    if sales == 0.0:
        result["gross_margin_pct"] = None
        result["division_by_zero"] = True
        result["margin_per_unit"] = None
        result["classification"] = None
        return result

    gross_margin_pct = round((sales - cogs) / sales * 100.0, 4)
    result["gross_margin_pct"] = gross_margin_pct
    result["division_by_zero"] = False

    # Clasificación
    if gross_margin_pct > 35.0:
        result["classification"] = "alta"
    elif gross_margin_pct > 15.0:
        result["classification"] = "media"
    else:
        result["classification"] = "baja"

    # Margen por unidad
    if units == 0:
        result["margin_per_unit"] = None
        result["units_division_by_zero"] = True
    else:
        result["margin_per_unit"] = round((sales - cogs) / units, 4)
        result["units_division_by_zero"] = False

    return result


# ── VPN simple ───────────────────────────────────────────────────────────────


def npv_simple(
    initial_investment: float,
    monthly_cashflows: list[float],
    discount_rate_annual: float,
) -> dict[str, Any]:
    """VPN e IRR de un proyecto con flujos mensuales (numpy-financial).

    Args:
        initial_investment: desembolso inicial (positivo → costo).
        monthly_cashflows: flujos mensuales (pueden ser negativos).
        discount_rate_annual: tasa de descuento anual (e.g. 0.12 para 12%).

    Returns:
        {npv, irr_monthly, irr_annual, recommendation} o
        {status: "insufficient_data"} si monthly_cashflows está vacío.

    Notas:
        - irr_monthly/irr_annual pueden ser None si numpy_financial.irr devuelve nan.
        - recommendation: "favorable" si npv > 0, "desfavorable" si npv <= 0.
        - La tasa mensual = (1 + rate_annual)^(1/12) - 1.
    """
    if not monthly_cashflows:
        return {"status": "insufficient_data", "reason": "monthly_cashflows vacío"}

    monthly_rate = (1.0 + discount_rate_annual) ** (1.0 / 12.0) - 1.0

    # numpy_financial.npv recibe: rate, [CF_0, CF_1, ..., CF_n]
    # CF_0 es el flujo en t=0 (la inversión como negativo)
    cashflows = [-initial_investment] + monthly_cashflows
    npv_val = float(npf.npv(monthly_rate, cashflows))

    # IRR: puede devolver nan si no converge (flujos todos negativos, etc.)
    irr_raw = npf.irr(cashflows)
    irr_monthly: float | None = None
    irr_annual: float | None = None

    if irr_raw is not None and not math.isnan(float(irr_raw)):
        irr_monthly = round(float(irr_raw), 6)
        irr_annual = round((1.0 + float(irr_raw)) ** 12.0 - 1.0, 6)

    recommendation = "favorable" if npv_val > 0.0 else "desfavorable"

    return {
        "npv": round(npv_val, 2),
        "irr_monthly": irr_monthly,
        "irr_annual": irr_annual,
        "recommendation": recommendation,
    }


# ── Detección de anomalías por z-score ──────────────────────────────────────


def detect_anomalies(
    values: list[float],
    z_threshold: float = 2.5,
) -> list[dict[str, Any]]:
    """Detecta valores anómalos en una serie mediante z-score (numpy puro, sin scipy).

    Args:
        values: serie numérica.
        z_threshold: umbral de z-score para considerar anomalía (default 2.5).

    Returns:
        Lista de {index, value, z_score, type} donde type es "pico" (z > 0)
        o "caida" (z < 0). Lista vacía si n < 3 o std == 0.
    """
    n = len(values)
    if n < 3:
        return []

    arr = np.array(values, dtype=np.float64)
    mean = float(np.mean(arr))
    std = float(np.std(arr, ddof=0))

    if std == 0.0:
        return []

    z_scores = (arr - mean) / std
    anomalies: list[dict[str, Any]] = []
    for i, (val, z) in enumerate(zip(values, z_scores, strict=False)):
        z_float = float(z)
        if abs(z_float) >= z_threshold:
            anomaly_type = "pico" if z_float > 0 else "caida"
            anomalies.append(
                {
                    "index": i,
                    "value": val,
                    "z_score": round(z_float, 4),
                    "type": anomaly_type,
                }
            )

    return anomalies


# ── Concentración (Pareto / dependencia) ─────────────────────────────────────

_MIN_SAMPLE_CONCENTRATION = 5


def concentration(
    values: list[float],
    *,
    min_required: int = _MIN_SAMPLE_CONCENTRATION,
) -> dict[str, Any]:
    """Mide qué tan concentrado está un total en pocas entidades.

    Pensado para distribuciones tipo Pareto (montos por cliente / proveedor /
    plataforma), NO para series temporales. Responde "¿dependo de pocos?" con
    top-share y HHI. Determinística, sin LLM.

    Args:
        values: montos por entidad (float). El orden no importa.
        min_required: muestra mínima para reportar (default 5).

    Returns:
        {n, total, zero_total, top1_share, top3_share, hhi} con shares en [0, 1], o
        {status: "insufficient_data", n, min_required} si n < min_required.

    Notas:
        - total == 0 → shares=None + zero_total=True (no dividir por cero; nunca
          inf/nan — regla no-invention).
        - Valores negativos se clampean a 0 (un monto por entidad no es negativo).
        - hhi = Σ(share_i²): ~0 atomizado, 1.0 monopolio (una sola entidad).
    """
    n = len(values)
    if n < min_required:
        return {"status": "insufficient_data", "n": n, "min_required": min_required}

    arr = np.maximum(np.array(values, dtype=np.float64), 0.0)
    total = float(np.sum(arr))

    if total == 0.0:
        return {
            "n": n,
            "total": 0.0,
            "zero_total": True,
            "top1_share": None,
            "top3_share": None,
            "hhi": None,
        }

    ordered = np.sort(arr)[::-1]  # descendente
    shares = ordered / total
    top1 = float(shares[0])
    top3 = float(np.sum(shares[:3]))
    hhi = float(np.sum(shares**2))

    return {
        "n": n,
        "total": round(total, 2),
        "zero_total": False,
        "top1_share": round(top1, 4),
        "top3_share": round(top3, 4),
        "hhi": round(hhi, 4),
    }
