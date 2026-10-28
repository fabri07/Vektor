"""Forecast de caja — proyección estadística de ingresos y egresos."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import get_current_tenant, require_active_subscription_for_insights
from app.application.services.forecast_service import ForecastResult, get_forecast
from app.persistence.db.redis_client import get_redis
from app.persistence.db.session import get_db_session
from app.persistence.models.tenant import Tenant

# B14: todo este router es análisis — con la suscripción bloqueada, 402.
router = APIRouter(dependencies=[Depends(require_active_subscription_for_insights)])


class ForecastPointResponse(BaseModel):
    date: str
    income: float
    expense: float
    net: float


class CashForecastResponse(BaseModel):
    tier: int
    confidence: str
    data_days: int
    horizon_days: int
    points: list[ForecastPointResponse]
    message: str | None = None
    # D-3/R-10: vendido con tarjeta de crédito que a hoy todavía no se acreditó
    # (venta + N días > hoy). La proyección lo suma el día que se acredita.
    credit_card_pending_ars: float = 0.0
    # A cuántos días corridos de la venta se cuenta el crédito (configurable en
    # /settings/health-config).
    credit_card_settlement_days: int


@router.get("/cash", response_model=CashForecastResponse, summary="Proyección de caja")
async def get_cash_forecast(
    refresh: bool = Query(default=False, description="Forzar recálculo ignorando caché"),
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db_session),
    redis: Redis = Depends(get_redis),
) -> CashForecastResponse:
    """
    Proyecta ingresos y egresos diarios para los próximos días.

    El horizonte y la confianza dependen de la cantidad de datos disponibles:
    - Tier 1 (14-30 días): 14 días de proyección, confianza BAJA
    - Tier 2 (30-90 días): 30 días de proyección, confianza MEDIA
    - Tier 3 (90+ días): 60 días de proyección, confianza ALTA

    Lo vendido con tarjeta de crédito entra N días corridos después de la venta
    (``credit_card_settlement_days``, por defecto 14).
    """
    result: ForecastResult = await get_forecast(
        tenant_id=tenant.tenant_id,
        db=session,
        redis=redis,
        force_refresh=refresh,
    )
    return CashForecastResponse(
        tier=result.tier,
        confidence=result.confidence,
        data_days=result.data_days,
        horizon_days=result.horizon_days,
        points=[
            ForecastPointResponse(
                date=p.date,
                income=p.income,
                expense=p.expense,
                net=p.net,
            )
            for p in result.points
        ],
        message=result.message,
        credit_card_pending_ars=result.credito_sin_acreditar_ars,
        credit_card_settlement_days=result.credit_card_settlement_days,
    )
