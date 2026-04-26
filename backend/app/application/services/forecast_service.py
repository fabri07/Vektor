"""Servicio de forecast de caja.

Algoritmo en 3 tiers según datos disponibles:
  Tier 0 — < 14 días: insuficiente, no proyecta
  Tier 1 — 14-30 días: promedio simple 7d proyectado 14 días
  Tier 2 — 30-90 días: EWMA + ajuste por día de semana, horizonte 30 días
  Tier 3 — 90+ días: patrón semanal + tendencia lineal, horizonte 60 días

No requiere tabla propia en esta fase: los resultados se cachean en Redis (TTL 6h).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from decimal import Decimal
from uuid import UUID

from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.observability.logger import get_logger
from app.persistence.models.transaction import ExpenseEntry, SaleEntry

logger = get_logger(__name__)

_CACHE_TTL = 60 * 60 * 6  # 6 horas


@dataclass
class ForecastPoint:
    date: str
    income: float
    expense: float
    net: float


@dataclass
class ForecastResult:
    tier: int
    confidence: str          # HIGH | MEDIUM | LOW
    data_days: int
    horizon_days: int
    points: list[ForecastPoint]
    message: str | None = None


def _cache_key(tenant_id: UUID) -> str:
    return f"forecast:cash:{tenant_id}"


async def get_forecast(
    tenant_id: UUID,
    db: AsyncSession,
    redis: Redis,
    force_refresh: bool = False,
) -> ForecastResult:
    """Devuelve el forecast cacheado o lo recalcula si expiró o no existe."""
    key = _cache_key(tenant_id)

    if not force_refresh:
        cached = await redis.get(key)
        if cached:
            try:
                return _deserialize(cached)
            except Exception:
                pass

    result = await _compute_forecast(tenant_id, db)

    try:
        await redis.setex(key, _CACHE_TTL, _serialize(result))
    except Exception as exc:
        logger.warning("forecast.cache_write_failed", error=str(exc))

    return result


async def _compute_forecast(tenant_id: UUID, db: AsyncSession) -> ForecastResult:
    """Obtiene datos de los últimos 180 días y calcula el forecast."""
    today = date.today()
    window_start = today - timedelta(days=180)

    # Agregados diarios de ingresos
    income_q = (
        select(SaleEntry.transaction_date, func.sum(SaleEntry.amount).label("total"))
        .where(
            SaleEntry.tenant_id == tenant_id,
            SaleEntry.transaction_date >= window_start,
        )
        .group_by(SaleEntry.transaction_date)
    )
    income_rows = (await db.execute(income_q)).all()

    # Agregados diarios de egresos
    expense_q = (
        select(ExpenseEntry.transaction_date, func.sum(ExpenseEntry.amount).label("total"))
        .where(
            ExpenseEntry.tenant_id == tenant_id,
            ExpenseEntry.transaction_date >= window_start,
        )
        .group_by(ExpenseEntry.transaction_date)
    )
    expense_rows = (await db.execute(expense_q)).all()

    daily_income: dict[date, float] = {r.transaction_date: float(r.total or 0) for r in income_rows}
    daily_expense: dict[date, float] = {r.transaction_date: float(r.total or 0) for r in expense_rows}

    all_dates = sorted(set(list(daily_income.keys()) + list(daily_expense.keys())))

    if not all_dates:
        return ForecastResult(
            tier=0, confidence="LOW", data_days=0, horizon_days=0, points=[],
            message="Sin datos suficientes para proyectar.",
        )

    data_days = (all_dates[-1] - all_dates[0]).days + 1

    if data_days < 14:
        return ForecastResult(
            tier=0, confidence="LOW", data_days=data_days, horizon_days=0, points=[],
            message=f"Necesitás al menos 14 días de datos para proyectar (tenés {data_days}).",
        )

    # Promedios globales
    total_days = max(data_days, 1)
    avg_income = sum(daily_income.values()) / total_days
    avg_expense = sum(daily_expense.values()) / total_days

    if data_days >= 30:
        # Ajuste por día de semana
        wd_income_sum = [0.0] * 7
        wd_income_cnt = [0] * 7
        for d, v in daily_income.items():
            wd = d.weekday()
            wd_income_sum[wd] += v
            wd_income_cnt[wd] += 1

        wd_mult = [1.0] * 7
        for wd in range(7):
            if wd_income_cnt[wd] > 0 and avg_income > 0:
                wd_avg = wd_income_sum[wd] / wd_income_cnt[wd]
                wd_mult[wd] = wd_avg / avg_income

        # Tendencia lineal simple (solo Tier 3, 90+ días)
        trend_daily = 0.0
        if data_days >= 90:
            recent_30 = [v for d, v in daily_income.items() if d >= today - timedelta(days=30)]
            old_30 = [v for d, v in daily_income.items() if d <= today - timedelta(days=60)]
            if recent_30 and old_30:
                recent_avg = sum(recent_30) / len(recent_30)
                old_avg = sum(old_30) / len(old_30)
                trend_daily = (recent_avg - old_avg) / 60
    else:
        wd_mult = [1.0] * 7
        trend_daily = 0.0

    tier = 1 if data_days < 30 else (2 if data_days < 90 else 3)
    horizon = 14 if tier == 1 else (30 if tier == 2 else 60)
    confidence = "LOW" if tier == 1 else ("MEDIUM" if tier == 2 else "HIGH")

    points: list[ForecastPoint] = []
    for i in range(1, horizon + 1):
        future = today + timedelta(days=i)
        wd = future.weekday()
        trend_adj = trend_daily * i if tier == 3 else 0.0
        proj_income = max(0.0, avg_income * wd_mult[wd] + trend_adj)
        points.append(ForecastPoint(
            date=future.isoformat(),
            income=round(proj_income),
            expense=round(avg_expense),
            net=round(proj_income - avg_expense),
        ))

    return ForecastResult(
        tier=tier,
        confidence=confidence,
        data_days=data_days,
        horizon_days=horizon,
        points=points,
    )


def _serialize(result: ForecastResult) -> str:
    d = asdict(result)
    return json.dumps(d)


def _deserialize(raw: bytes | str) -> ForecastResult:
    d = json.loads(raw)
    points = [ForecastPoint(**p) for p in d.pop("points", [])]
    return ForecastResult(**d, points=points)
