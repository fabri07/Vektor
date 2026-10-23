"""Servicio de días y horarios laborales por tenant (Sprint 20).

Persiste en business_profiles (work_days, work_open_hour, work_close_hour).
Cuando los campos son NULL (cuenta nueva o pre-migración) sirve defaults:
Lun-Sáb 09-18. Nunca usar `or` con hora/día (0 y día 0=lunes son válidos).

Lo consume:
  - el router /settings/work-schedule (GET/PATCH)
  - el onboarding (persistencia inicial)
  - CashCloseService (F3) vía `resolve_schedule` para el highlight del botón.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.observability.logger import get_logger
from app.persistence.models.audit import DecisionAuditLog
from app.persistence.models.business import BusinessProfile

logger = get_logger(__name__)

# Defaults: Lun-Sáb 09:00-18:00 (0=lunes … 5=sábado)
DEFAULT_WORK_DAYS: list[int] = [0, 1, 2, 3, 4, 5]
DEFAULT_OPEN_HOUR = 9
DEFAULT_CLOSE_HOUR = 18


class WorkScheduleRequest(BaseModel):
    work_days: list[int] = Field(..., min_length=1)
    work_open_hour: int = Field(..., ge=0, le=23)
    work_close_hour: int = Field(..., ge=0, le=23)

    @field_validator("work_days")
    @classmethod
    def _valid_days(cls, v: list[int]) -> list[int]:
        if any(d < 0 or d > 6 for d in v):
            raise ValueError("work_days deben estar en 0-6 (0=lunes … 6=domingo)")
        deduped = sorted(set(v))
        return deduped

    @model_validator(mode="after")
    def _close_after_open(self) -> WorkScheduleRequest:
        if self.work_close_hour <= self.work_open_hour:
            raise ValueError("work_close_hour debe ser mayor que work_open_hour")
        return self


class WorkScheduleResponse(BaseModel):
    work_days: list[int]
    work_open_hour: int
    work_close_hour: int
    is_default: bool  # True si nunca se configuró (sirviendo defaults)


def resolve_schedule(bp: BusinessProfile | None) -> tuple[list[int], int, int]:
    """Devuelve (work_days, open_hour, close_hour) con defaults si falta config.

    Null-check explícito por campo — hora 0 y día 0 son válidos, nunca usar `or`.
    """
    if bp is None:
        return DEFAULT_WORK_DAYS, DEFAULT_OPEN_HOUR, DEFAULT_CLOSE_HOUR
    days = bp.work_days if bp.work_days is not None else DEFAULT_WORK_DAYS
    open_hour = bp.work_open_hour if bp.work_open_hour is not None else DEFAULT_OPEN_HOUR
    close_hour = bp.work_close_hour if bp.work_close_hour is not None else DEFAULT_CLOSE_HOUR
    return days, open_hour, close_hour


class WorkScheduleService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def _get_profile(self, tenant_id: UUID) -> BusinessProfile | None:
        result = await self._session.execute(
            select(BusinessProfile).where(BusinessProfile.tenant_id == tenant_id)
        )
        return result.scalar_one_or_none()

    async def get(self, tenant_id: UUID) -> WorkScheduleResponse:
        bp = await self._get_profile(tenant_id)
        days, open_hour, close_hour = resolve_schedule(bp)
        is_default = bp is None or bp.work_days is None
        return WorkScheduleResponse(
            work_days=days,
            work_open_hour=open_hour,
            work_close_hour=close_hour,
            is_default=is_default,
        )

    async def update(
        self,
        tenant_id: UUID,
        user_id: UUID,
        config: WorkScheduleRequest,
    ) -> WorkScheduleResponse:
        bp = await self._get_profile(tenant_id)
        if bp is None:
            raise ValueError("BusinessProfile no encontrado para el tenant")

        before = {
            "work_days": bp.work_days,
            "work_open_hour": bp.work_open_hour,
            "work_close_hour": bp.work_close_hour,
        }
        bp.work_days = config.work_days
        bp.work_open_hour = config.work_open_hour
        bp.work_close_hour = config.work_close_hour

        self._session.add(
            DecisionAuditLog(
                tenant_id=tenant_id,
                decision_type="WORK_SCHEDULE_UPDATED",
                decision_data={
                    "before": before,
                    "after": {
                        "work_days": config.work_days,
                        "work_open_hour": config.work_open_hour,
                        "work_close_hour": config.work_close_hour,
                    },
                    "source": "ui",
                },
                triggered_by="ui:settings",
                actor_user_id=user_id,
                context={"endpoint": "settings/work-schedule"},
                created_at=datetime.now(UTC),
            )
        )
        await self._session.commit()
        logger.info(
            "work_schedule.updated",
            tenant_id=str(tenant_id),
            work_days=config.work_days,
            work_open_hour=config.work_open_hour,
            work_close_hour=config.work_close_hour,
        )
        return WorkScheduleResponse(
            work_days=config.work_days,
            work_open_hour=config.work_open_hour,
            work_close_hour=config.work_close_hour,
            is_default=False,
        )
