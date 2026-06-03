"""Forecast de caja — proyección estadística de ingresos y egresos."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import get_current_tenant
from app.application.services.forecast_service import ForecastResult, get_forecast
from app.persistence.db.redis_client import get_redis
from app.persistence.db.session import get_db_session
from app.persistence.models.tenant import Tenant

router = APIRouter()


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
    )
