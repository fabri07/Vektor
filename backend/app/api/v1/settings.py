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

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import (
    get_current_user,
    require_modify_access,
    require_owner_stepup,
)
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
from app.domain.fiscal_condition import (
    FiscalCondition,
    normalize_fiscal_condition,
)
from app.persistence.db.session import get_db_session
from app.persistence.models.business import BusinessProfile
from app.persistence.models.user import User
from app.persistence.repositories.user_repository import UserRepository

router = APIRouter()


class FiscalConditionResponse(BaseModel):
    # 'monotributo' | 'responsable_inscripto' | 'informal' | None = no configurado.
    # El legacy 'registered' se normaliza a 'monotributo' al leerlo (additive).
    fiscal_condition: FiscalCondition | None


class FiscalConditionRequest(BaseModel):
    fiscal_condition: FiscalCondition | None


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
    current_user: User = Depends(require_modify_access),
    db: AsyncSession = Depends(get_db_session),
) -> HealthConfigResponse:
    """Actualiza los objetivos de margen del tenant. Requiere permiso de modificación + PIN."""
    svc = HealthConfigService(db)
    return await svc.update(current_user.tenant_id, body)


@router.delete("/health-config", response_model=HealthConfigResponse)
async def reset_health_config(
    current_user: User = Depends(require_modify_access),
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
    current_user: User = Depends(require_modify_access),
    db: AsyncSession = Depends(get_db_session),
) -> WorkScheduleResponse:
    """Actualiza días y horarios laborales del tenant. Requiere permiso de modificación + PIN."""
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
    """Condición fiscal del negocio — solo informativa (heurísticas + guía del arqueo).

    Normaliza el valor persistido (incluido el legacy 'registered') a un código
    canónico antes de devolverlo, sin reescribir la fila.
    """
    profile = await _get_business_profile(db, current_user.tenant_id)
    return FiscalConditionResponse(
        fiscal_condition=normalize_fiscal_condition(profile.fiscal_condition)
    )


@router.patch("/fiscal-condition", response_model=FiscalConditionResponse)
async def update_fiscal_condition(
    body: FiscalConditionRequest,
    current_user: User = Depends(require_modify_access),
    db: AsyncSession = Depends(get_db_session),
) -> FiscalConditionResponse:
    """Actualiza la condición fiscal. Requiere permiso de modificación + PIN.

    Persiste el valor canónico (Pydantic ya restringe a los 3 valores o None).
    """
    profile = await _get_business_profile(db, current_user.tenant_id)
    profile.fiscal_condition = body.fiscal_condition
    await db.flush()
    await db.commit()
    return FiscalConditionResponse(fiscal_condition=body.fiscal_condition)


# ── Permisos de equipo (sub-cuentas) ────────────────────────────────────────────


class TeamMemberResponse(BaseModel):
    user_id: UUID
    email: str
    full_name: str
    role_code: str
    can_modify_sensitive: bool
    pin_set: bool


class TeamPermissionRequest(BaseModel):
    can_modify_sensitive: bool


@router.get("/team", response_model=list[TeamMemberResponse])
async def list_team(
    current_user: User = Depends(require_owner_stepup),
    db: AsyncSession = Depends(get_db_session),
) -> list[TeamMemberResponse]:
    """Lista las cuentas del tenant con su permiso de modificación. Solo OWNER."""
    users = await UserRepository(db).list_by_tenant(current_user.tenant_id)
    return [
        TeamMemberResponse(
            user_id=u.user_id,
            email=u.email,
            full_name=u.full_name,
            role_code=u.role_code,
            can_modify_sensitive=u.can_modify_sensitive,
            pin_set=u.pin_hash is not None,
        )
        for u in users
    ]


@router.patch("/team/{user_id}", response_model=TeamMemberResponse)
async def update_team_permission(
    user_id: UUID,
    body: TeamPermissionRequest,
    current_user: User = Depends(require_owner_stepup),
    db: AsyncSession = Depends(get_db_session),
) -> TeamMemberResponse:
    """Otorga/revoca a una sub-cuenta el permiso de modificar datos. Solo OWNER."""
    repo = UserRepository(db)
    target = await repo.get_by_id(user_id, current_user.tenant_id)
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Usuario no encontrado.")
    if target.role_code == "OWNER":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="El dueño ya tiene permiso total; no se modifica.",
        )
    target.can_modify_sensitive = body.can_modify_sensitive
    await repo.save(target)
    await db.commit()
    return TeamMemberResponse(
        user_id=target.user_id,
        email=target.email,
        full_name=target.full_name,
        role_code=target.role_code,
        can_modify_sensitive=target.can_modify_sensitive,
        pin_set=target.pin_hash is not None,
    )
