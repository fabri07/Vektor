"""Settings router — configuración del tenant.

Endpoints:
  GET  /settings/health-config       → obtener configuración de margen
  PATCH /settings/health-config      → actualizar objetivo de margen
  DELETE /settings/health-config     → resetear a valores por vertical
  GET  /settings/work-schedule       → obtener días y horarios laborales
  PATCH /settings/work-schedule      → actualizar días y horarios laborales
  GET  /settings/fiscal-condition    → condición fiscal (guía del arqueo)
  PATCH /settings/fiscal-condition   → actualizar condición fiscal
"""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import get_current_user, require_role
from app.application.services.health_config_service import (
    HealthConfigRequest,
    HealthConfigResponse,
    HealthConfigService,
)
from app.application.services.work_schedule_service import (
    WorkScheduleRequest,
    WorkScheduleResponse,
    WorkScheduleService,
)
from app.persistence.db.session import get_db_session
from app.persistence.models.business import BusinessProfile
from app.persistence.models.user import User

router = APIRouter()


class FiscalConditionResponse(BaseModel):
    # 'registered' (monotributo/RI) | 'informal' | None = no configurado.
    fiscal_condition: Literal["registered", "informal"] | None


class FiscalConditionRequest(BaseModel):
    fiscal_condition: Literal["registered", "informal"] | None


@router.get("/health-config", response_model=HealthConfigResponse)
async def get_health_config(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> HealthConfigResponse:
    """Obtiene la configuración de margen del tenant."""
    svc = HealthConfigService(db)
    return await svc.get(current_user.tenant_id)


@router.patch("/health-config", response_model=HealthConfigResponse)
async def update_health_config(
    body: HealthConfigRequest,
    current_user: User = Depends(require_role("OWNER", "ADMIN")),
    db: AsyncSession = Depends(get_db_session),
) -> HealthConfigResponse:
    """Actualiza los objetivos de margen del tenant. Requiere OWNER o ADMIN."""
    svc = HealthConfigService(db)
    return await svc.update(current_user.tenant_id, body)


@router.delete("/health-config", response_model=HealthConfigResponse)
async def reset_health_config(
    current_user: User = Depends(require_role("OWNER", "ADMIN")),
    db: AsyncSession = Depends(get_db_session),
) -> HealthConfigResponse:
    """Resetea la configuración de margen a los valores por vertical."""
    svc = HealthConfigService(db)
    return await svc.reset(current_user.tenant_id)


@router.get("/work-schedule", response_model=WorkScheduleResponse)
async def get_work_schedule(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> WorkScheduleResponse:
    """Obtiene los días y horarios laborales del tenant (defaults si no configurado)."""
    svc = WorkScheduleService(db)
    return await svc.get(current_user.tenant_id)


@router.patch("/work-schedule", response_model=WorkScheduleResponse)
async def update_work_schedule(
    body: WorkScheduleRequest,
    current_user: User = Depends(require_role("OWNER", "ADMIN")),
    db: AsyncSession = Depends(get_db_session),
) -> WorkScheduleResponse:
    """Actualiza días y horarios laborales del tenant. Requiere OWNER o ADMIN."""
    svc = WorkScheduleService(db)
    try:
        return await svc.update(current_user.tenant_id, current_user.user_id, body)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


async def _get_business_profile(db: AsyncSession, tenant_id: object) -> BusinessProfile:
    profile = (
        await db.execute(
            select(BusinessProfile).where(BusinessProfile.tenant_id == tenant_id)
        )
    ).scalar_one_or_none()
    if profile is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="El tenant no tiene perfil de negocio.",
        )
    return profile


@router.get("/fiscal-condition", response_model=FiscalConditionResponse)
async def get_fiscal_condition(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> FiscalConditionResponse:
    """Condición fiscal del negocio — solo adapta la guía del arqueo en la UI."""
    profile = await _get_business_profile(db, current_user.tenant_id)
    return FiscalConditionResponse(fiscal_condition=profile.fiscal_condition)


@router.patch("/fiscal-condition", response_model=FiscalConditionResponse)
async def update_fiscal_condition(
    body: FiscalConditionRequest,
    current_user: User = Depends(require_role("OWNER", "ADMIN")),
    db: AsyncSession = Depends(get_db_session),
) -> FiscalConditionResponse:
    """Actualiza la condición fiscal. Requiere OWNER o ADMIN."""
    profile = await _get_business_profile(db, current_user.tenant_id)
    profile.fiscal_condition = body.fiscal_condition
    await db.flush()
    await db.commit()
    return FiscalConditionResponse(fiscal_condition=body.fiscal_condition)
