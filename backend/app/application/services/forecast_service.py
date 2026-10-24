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
from uuid import UUID

from redis.asyncio import Redis
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.facts_service import FIADO_METHODS
from app.observability.logger import get_logger
from app.persistence.models.transaction import ExpenseEntry, SaleEntry
from app.persistence.repositories._pos_tender_scope import tenders_mixtos, venta_no_mixta
from app.persistence.repositories._ventas_scope import es_cobro_de_fiado, es_venta
from app.persistence.repositories.owner_capital_repository import flujo_de_dinero

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
    confidence: str  # HIGH | MEDIUM | LOW
    data_days: int
    horizon_days: int
    points: list[ForecastPoint]
    message: str | None = None


def _cache_key(tenant_id: UUID) -> str:
    # v2 (T1): el ingreso de caja dejó de contar el fiado y dos veces los
    # cobros; un blob viejo serviría 6 h la proyección inflada.
    return f"forecast:cash:v2:{tenant_id}"


def _dia(v: object) -> date:
    """`func.date()` devuelve date (Postgres) o 'YYYY-MM-DD' (SQLite)."""
    return date.fromisoformat(str(v)[:10])


async def ingresos_de_caja_por_dia(
    tenant_id: UUID, db: AsyncSession, desde: date
) -> dict[date, float]:
    """Plata que ENTRA a caja cada día desde ``desde`` (T1).

    Es una proyección de caja, no de ventas: entra toda venta MENOS el fiado
    (``FIADO_METHODS``: es deuda a cobrar), la parte no fiada de un pago mixto de
    caja (B9) y los cobros de fiado el día que se cobran. Antes sumaba toda venta
    más los cobros: una venta fiada de $1.000 cobrada después daba $2.000 de
    ingreso de caja. Es la resta de ``FactsService._split_liquidez`` salvo la
    tarjeta de crédito, que la proyección sigue contando el día de la venta
    (FactsService la saca de la caja de hoy; correrla a su acreditación es una
    decisión aparte).
    """
    dia_venta = func.date(SaleEntry.transaction_date).label("day")
    ventas_q = (
        select(dia_venta, func.sum(SaleEntry.amount).label("total"))
        .where(
            SaleEntry.tenant_id == tenant_id,
            SaleEntry.voided_at.is_(None),
            SaleEntry.transaction_date >= desde,
            or_(
                es_cobro_de_fiado(),
                and_(
                    es_venta(),
                    venta_no_mixta(),
                    func.lower(SaleEntry.payment_method).not_in(FIADO_METHODS),
                ),
            ),
        )
        .group_by(dia_venta)
    )
    mixtos = tenders_mixtos(tenant_id, desde)
    dia_mixto = func.date(mixtos.c.sale_date).label("day")
    mixtos_q = (
        select(dia_mixto, func.sum(mixtos.c.amount_ars).label("total"))
        .where(func.lower(mixtos.c.payment_method).not_in(FIADO_METHODS))
        .group_by(dia_mixto)
    )
    por_dia: dict[date, float] = {}
    for q in (ventas_q, mixtos_q):
        for r in (await db.execute(q)).all():
            d = _dia(r.day)
            por_dia[d] = por_dia.get(d, 0.0) + float(r.total or 0)
    return por_dia


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

    # Agregados DIARIOS: agrupar por func.date() para no fragmentar por hora/minuto
    # ahora que transaction_date es un timestamp.
    daily_income = await ingresos_de_caja_por_dia(tenant_id, db, window_start)

    expense_day = func.date(ExpenseEntry.transaction_date).label("day")
    expense_q = (
        select(expense_day, func.sum(ExpenseEntry.amount).label("total"))
        .where(
            ExpenseEntry.tenant_id == tenant_id,
            ExpenseEntry.voided_at.is_(None),
            ExpenseEntry.transaction_date >= window_start,
        )
        .group_by(expense_day)
    )
    expense_rows = (await db.execute(expense_q)).all()

    daily_expense: dict[date, float] = {_dia(r.day): float(r.total or 0) for r in expense_rows}
    # Capital de socios (D·2): la proyección es de CAJA, y un retiro en dinero
    # sale de la caja igual que cuando era un gasto PAYROLL. Los aportes no se
    # proyectan: son plata del socio, no ingreso del negocio.
    for f in await flujo_de_dinero(db, tenant_id, window_start, today):
        if f.salidas:
            daily_expense[f.dia] = daily_expense.get(f.dia, 0.0) + float(f.salidas)

    all_dates = sorted(set(list(daily_income.keys()) + list(daily_expense.keys())))

    if not all_dates:
        return ForecastResult(
            tier=0,
            confidence="LOW",
            data_days=0,
            horizon_days=0,
            points=[],
            message="Sin datos suficientes para proyectar.",
        )

    data_days = (all_dates[-1] - all_dates[0]).days + 1
    latest_data_date = all_dates[-1]
    is_stale = (today - latest_data_date).days > 7

    if data_days < 14:
        return ForecastResult(
            tier=0,
            confidence="LOW",
            data_days=data_days,
            horizon_days=0,
            points=[],
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
    message = None
    if is_stale:
        confidence = "LOW"
        message = (
            f"El último dato disponible es del {latest_data_date.isoformat()}; "
            "la proyección puede no reflejar la actividad actual."
        )

    points: list[ForecastPoint] = []
    for i in range(1, horizon + 1):
        future = today + timedelta(days=i)
        wd = future.weekday()
        trend_adj = trend_daily * i if tier == 3 else 0.0
        proj_income = max(0.0, avg_income * wd_mult[wd] + trend_adj)
        points.append(
            ForecastPoint(
                date=future.isoformat(),
                income=round(proj_income),
                expense=round(avg_expense),
                net=round(proj_income - avg_expense),
            )
        )

    return ForecastResult(
        tier=tier,
        confidence=confidence,
        data_days=data_days,
        horizon_days=horizon,
        points=points,
        message=message,
    )


def _serialize(result: ForecastResult) -> str:
    d = asdict(result)
    return json.dumps(d)


def _deserialize(raw: bytes | str) -> ForecastResult:
    d = json.loads(raw)
    points = [ForecastPoint(**p) for p in d.pop("points", [])]
    return ForecastResult(**d, points=points)
