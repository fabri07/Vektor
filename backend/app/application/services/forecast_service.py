"""Servicio de forecast de caja.

Algoritmo en 3 tiers según datos disponibles:
  Tier 0 — < 14 días: insuficiente, no proyecta
  Tier 1 — 14-30 días: promedio simple 7d proyectado 14 días
  Tier 2 — 30-90 días: EWMA + ajuste por día de semana, horizonte 30 días
  Tier 3 — 90+ días: patrón semanal + tendencia lineal, horizonte 60 días

No requiere tabla propia en esta fase: los resultados se cachean en Redis (TTL 6h).

Tarjeta de crédito (D-3/R-10, decisión del dueño 2026-10-28): lo vendido con
crédito entra a caja N días corridos después de la venta (N por tenant, por
defecto 14; `health_config_service.get_credit_card_settlement_days`). Cada día
futuro suma el crédito vendido N días antes: el real si ese día ya pasó; si no,
el esperado según el modelo de la serie de crédito. Las dos series se modelan
sobre el mismo calendario (tendencia por día corrido, no por fila), así que el
modelo es lineal en la serie y con N = 0 la proyección da lo mismo que contar el
crédito como caja el mismo día, en todos los tiers (N-1, revalidación #217).
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from uuid import UUID

from redis.asyncio import Redis
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.facts_service import DELAYED_METHODS, FIADO_METHODS
from app.application.services.health_config_service import get_credit_card_settlement_days
from app.domain.business_time import today_ar
from app.observability.logger import get_logger
from app.persistence.models.transaction import ExpenseEntry, SaleEntry
from app.persistence.repositories._pos_tender_scope import tenders_mixtos, venta_no_mixta
from app.persistence.repositories._ventas_scope import es_cobro_de_fiado, es_venta
from app.persistence.repositories.owner_capital_repository import flujo_de_dinero

logger = get_logger(__name__)

_CACHE_TTL = 60 * 60 * 6  # 6 horas
#: Tope de N (`HealthConfigRequest.credit_card_settlement_days`).
_MAX_SETTLEMENT_DAYS = 60

#: Ventas que NO son plata en caja el día de la venta: el fiado (deuda a cobrar)
#: y la tarjeta de crédito (se acredita N días después; D-3/R-10). Es la resta de
#: ``FactsService._split_liquidez``.
_FUERA_DE_CAJA_INMEDIATA = FIADO_METHODS | DELAYED_METHODS


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
    # D-3/R-10: lo vendido con tarjeta de crédito que a hoy todavía no se
    # acreditó (venta + N días > hoy). 0.0 es un valor medido.
    credito_sin_acreditar_ars: float = 0.0
    # A cuántos días corridos de la venta se cuenta el crédito (configurable).
    credit_card_settlement_days: int = 14


def _cache_key(tenant_id: UUID, settlement_days: int) -> str:
    # v2 (T1): el ingreso de caja dejó de contar el fiado y dos veces los
    # cobros; un blob viejo serviría 6 h la proyección inflada.
    # v3 (D-3/R-10): tampoco cuenta la tarjeta de crédito el día de la venta.
    # v4: el crédito entra a N días; N va en la clave para que cambiarlo no
    # sirva la proyección calculada con el N anterior.
    return f"forecast:cash:v4:{tenant_id}:n{settlement_days}"


async def invalidar_cache(tenant_id: UUID, redis: Redis) -> None:
    """Borra la proyección cacheada del tenant con todos los N posibles: la
    llaman PATCH/DELETE de /settings/health-config para no servir un blob de
    antes del cambio (volver a un N anterior reusaría su clave)."""
    try:
        await redis.delete(*(_cache_key(tenant_id, n) for n in range(_MAX_SETTLEMENT_DAYS + 1)))
    except Exception as exc:
        logger.warning("forecast.cache_invalidate_failed", error=str(exc))


def _dia(v: object) -> date:
    """`func.date()` devuelve date (Postgres) o 'YYYY-MM-DD' (SQLite)."""
    return date.fromisoformat(str(v)[:10])


async def ingresos_de_caja_por_dia(
    tenant_id: UUID, db: AsyncSession, desde: date
) -> dict[date, float]:
    """Plata que ENTRA a caja cada día desde ``desde`` (T1).

    Es una proyección de caja, no de ventas: entra toda venta MENOS el fiado
    (``FIADO_METHODS``: es deuda a cobrar) y MENOS la tarjeta de crédito
    (``DELAYED_METHODS``), la parte líquida de un pago mixto de caja (B9) y los
    cobros de fiado el día que se cobran. Antes sumaba toda venta más los
    cobros: una venta fiada de $1.000 cobrada después daba $2.000 de ingreso de
    caja. Es la resta de ``FactsService._split_liquidez``.

    La tarjeta de crédito (D-3/R-10) no entra acá: la proyección la suma aparte,
    N días después de la venta (``credito_por_dia``).
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
                    func.lower(SaleEntry.payment_method).not_in(_FUERA_DE_CAJA_INMEDIATA),
                ),
            ),
        )
        .group_by(dia_venta)
    )
    mixtos = tenders_mixtos(tenant_id, desde)
    dia_mixto = func.date(mixtos.c.sale_date).label("day")
    mixtos_q = (
        select(dia_mixto, func.sum(mixtos.c.amount_ars).label("total"))
        .where(func.lower(mixtos.c.payment_method).not_in(_FUERA_DE_CAJA_INMEDIATA))
        .group_by(dia_mixto)
    )
    por_dia: dict[date, float] = {}
    for q in (ventas_q, mixtos_q):
        for r in (await db.execute(q)).all():
            d = _dia(r.day)
            por_dia[d] = por_dia.get(d, 0.0) + float(r.total or 0)
    return por_dia


async def credito_por_dia(tenant_id: UUID, db: AsyncSession, desde: date) -> dict[date, float]:
    """Lo vendido con tarjeta de crédito cada día (fecha de la VENTA) desde
    ``desde`` (D-3/R-10): las ventas con ese método y la parte con crédito de un
    pago mixto, con el mismo alcance que ``ingresos_de_caja_por_dia``."""
    dia_venta = func.date(SaleEntry.transaction_date).label("day")
    ventas_q = (
        select(dia_venta, func.sum(SaleEntry.amount).label("total"))
        .where(
            SaleEntry.tenant_id == tenant_id,
            SaleEntry.voided_at.is_(None),
            SaleEntry.transaction_date >= desde,
            es_venta(),
            venta_no_mixta(),
            func.lower(SaleEntry.payment_method).in_(DELAYED_METHODS),
        )
        .group_by(dia_venta)
    )
    mixtos = tenders_mixtos(tenant_id, desde)
    dia_mixto = func.date(mixtos.c.sale_date).label("day")
    mixtos_q = (
        select(dia_mixto, func.sum(mixtos.c.amount_ars).label("total"))
        .where(func.lower(mixtos.c.payment_method).in_(DELAYED_METHODS))
        .group_by(dia_mixto)
    )
    por_dia: dict[date, float] = {}
    for q in (ventas_q, mixtos_q):
        for r in (await db.execute(q)).all():
            d = _dia(r.day)
            por_dia[d] = por_dia.get(d, 0.0) + float(r.total or 0)
    return por_dia


async def dias_con_ventas(tenant_id: UUID, db: AsyncSession, desde: date) -> set[date]:
    """Días con cualquier venta o cobro, con el método que sea (M-1/M-3): un
    día que sólo vendió con crédito o fiado es un día con datos."""
    dia = func.date(SaleEntry.transaction_date).label("day")
    q = (
        select(dia)
        .where(
            SaleEntry.tenant_id == tenant_id,
            SaleEntry.voided_at.is_(None),
            SaleEntry.transaction_date >= desde,
        )
        .group_by(dia)
    )
    return {_dia(r.day) for r in (await db.execute(q)).all()}


def credito_sin_acreditar(credito: dict[date, float], today: date, settlement_days: int) -> float:
    """Lo vendido con crédito que a ``today`` todavía no se acreditó: venta + N > hoy."""
    corte = today - timedelta(days=settlement_days)
    return sum(v for d, v in credito.items() if corte < d <= today)


async def get_forecast(
    tenant_id: UUID,
    db: AsyncSession,
    redis: Redis,
    force_refresh: bool = False,
) -> ForecastResult:
    """Devuelve el forecast cacheado o lo recalcula si expiró o no existe."""
    settlement_days = await get_credit_card_settlement_days(tenant_id, db)
    key = _cache_key(tenant_id, settlement_days)

    if not force_refresh:
        cached = await redis.get(key)
        if cached:
            try:
                return _deserialize(cached)
            except Exception:
                pass

    result = await _compute_forecast(tenant_id, db, settlement_days)

    try:
        await redis.setex(key, _CACHE_TTL, _serialize(result))
    except Exception as exc:
        logger.warning("forecast.cache_write_failed", error=str(exc))

    return result


class _Modelo:
    """Proyección de una serie diaria: promedio sobre los días de historial,
    ajuste por día de semana (tier ≥ 2) y tendencia lineal (tier 3)."""

    def __init__(
        self,
        serie: dict[date, float],
        data_days: int,
        tier: int,
        today: date,
        dias_de_venta: Iterable[date],
        inicio: date,
    ) -> None:
        self.avg = sum(serie.values()) / max(data_days, 1)
        self.wd_mult = [1.0] * 7
        self.trend_daily = 0.0
        self.tier = tier
        if tier < 2:
            return
        # m-1: el promedio de cada día de semana divide por los días que tuvieron
        # cualquier venta, no sólo los que tuvieron fila en esta serie.
        wd_sum = [0.0] * 7
        wd_cnt = [0] * 7
        for d in set(dias_de_venta) | set(serie):
            wd_cnt[d.weekday()] += 1
        for d, v in serie.items():
            wd_sum[d.weekday()] += v
        for wd in range(7):
            if wd_cnt[wd] > 0 and self.avg > 0:
                self.wd_mult[wd] = (wd_sum[wd] / wd_cnt[wd]) / self.avg
        if tier == 3:
            # N-1: la tendencia promedia sobre el CALENDARIO (día sin fila = 0),
            # no por fila: en una serie rala daba una pendiente «por día con
            # venta» que después se suma a todos los días (+26 % en tier 3). Así
            # el modelo es lineal: líquido + crédito = total.
            calendario = [inicio + timedelta(days=k) for k in range((today - inicio).days + 1)]
            recent_30 = [serie.get(d, 0.0) for d in calendario if d >= today - timedelta(days=30)]
            old_30 = [serie.get(d, 0.0) for d in calendario if d <= today - timedelta(days=60)]
            if recent_30 and old_30:
                recent_avg = sum(recent_30) / len(recent_30)
                old_avg = sum(old_30) / len(old_30)
                self.trend_daily = (recent_avg - old_avg) / 60

    def valor(self, dia: date, i: int) -> float:
        """Lo esperado para ``dia``, que está ``i`` días después de hoy."""
        trend_adj = self.trend_daily * i if self.tier == 3 else 0.0
        return max(0.0, self.avg * self.wd_mult[dia.weekday()] + trend_adj)


async def _compute_forecast(
    tenant_id: UUID, db: AsyncSession, settlement_days: int | None = None
) -> ForecastResult:
    """Obtiene datos de los últimos 180 días y calcula el forecast."""
    if settlement_days is None:
        settlement_days = await get_credit_card_settlement_days(tenant_id, db)
    today = today_ar()
    window_start = today - timedelta(days=180)

    # Agregados DIARIOS: agrupar por func.date() para no fragmentar por hora/minuto
    # ahora que transaction_date es un timestamp.
    # Borde superior duro: una fila con fecha futura no es historia (m-8).
    daily_income = {
        d: v for d, v in (await ingresos_de_caja_por_dia(tenant_id, db, window_start)).items()
        if d <= today
    }
    daily_credit = {
        d: v for d, v in (await credito_por_dia(tenant_id, db, window_start)).items()
        if d <= today
    }
    dias_de_venta = {d for d in await dias_con_ventas(tenant_id, db, window_start) if d <= today}
    pendiente = round(credito_sin_acreditar(daily_credit, today, settlement_days), 2)

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

    daily_expense: dict[date, float] = {
        _dia(r.day): float(r.total or 0) for r in expense_rows if _dia(r.day) <= today
    }
    # Capital de socios (D·2): la proyección es de CAJA, y un retiro en dinero
    # sale de la caja igual que cuando era un gasto PAYROLL. Los aportes no se
    # proyectan: son plata del socio, no ingreso del negocio.
    for f in await flujo_de_dinero(db, tenant_id, window_start, today):
        if f.salidas:
            daily_expense[f.dia] = daily_expense.get(f.dia, 0.0) + float(f.salidas)

    # M-1/M-2/M-3: los días con datos salen de TODAS las ventas (cualquier
    # método) y los gastos, no sólo del ingreso líquido.
    all_dates = sorted(dias_de_venta | set(daily_income) | set(daily_expense))

    if not all_dates:
        return ForecastResult(
            tier=0,
            confidence="LOW",
            data_days=0,
            horizon_days=0,
            points=[],
            message="Sin datos suficientes para proyectar.",
            credito_sin_acreditar_ars=pendiente,
            credit_card_settlement_days=settlement_days,
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
            credito_sin_acreditar_ars=pendiente,
            credit_card_settlement_days=settlement_days,
        )

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

    inicio = all_dates[0]
    liquido = _Modelo(daily_income, data_days, tier, today, dias_de_venta, inicio)
    credito = _Modelo(daily_credit, data_days, tier, today, dias_de_venta, inicio)
    avg_expense = sum(daily_expense.values()) / data_days

    points: list[ForecastPoint] = []
    for i in range(1, horizon + 1):
        future = today + timedelta(days=i)
        # El crédito que se acredita ``future`` es el vendido N días antes:
        # conocido si ese día ya pasó; si no, el esperado para ese día de venta.
        dia_de_venta = future - timedelta(days=settlement_days)
        if dia_de_venta <= today:
            credito_del_dia = daily_credit.get(dia_de_venta, 0.0)
        else:
            credito_del_dia = credito.valor(dia_de_venta, (dia_de_venta - today).days)
        proj_income = liquido.valor(future, i) + credito_del_dia
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
        credito_sin_acreditar_ars=pendiente,
        credit_card_settlement_days=settlement_days,
    )


def _serialize(result: ForecastResult) -> str:
    d = asdict(result)
    return json.dumps(d)


def _deserialize(raw: bytes | str) -> ForecastResult:
    d = json.loads(raw)
    points = [ForecastPoint(**p) for p in d.pop("points", [])]
    return ForecastResult(**d, points=points)
