"""Settings router — configuración del tenant.

Endpoints:
  GET  /settings/health-config       → obtener configuración de margen
  PATCH /settings/health-config      → actualizar objetivo de margen
  DELETE /settings/health-config     → resetear a valores por vertical
"""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import get_current_user, require_role
from app.application.services.health_config_service import (
    HealthConfigRequest,
    HealthConfigResponse,
    HealthConfigService,
)
from app.persistence.db.session import get_db_session
from app.persistence.models.user import User

router = APIRouter()


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
