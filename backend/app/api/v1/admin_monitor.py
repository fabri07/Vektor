"""
Monitor de plataforma — SUPERADMIN.

GET /api/v1/admin/monitor — foto agregada de todos los negocios (sólo lectura).

Como el resto de `/admin`, cruza tenants a propósito: no usa el `tenant_id` del JWT.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import require_role
from app.application.services import platform_monitor_service as svc
from app.persistence.db.session import get_db_session
from app.schemas.admin_monitor import MonitorResponse

router = APIRouter(dependencies=[Depends(require_role("SUPERADMIN"))])


@router.get("", response_model=MonitorResponse, summary="Monitor de la plataforma")
async def get_monitor(session: AsyncSession = Depends(get_db_session)) -> MonitorResponse:
    return await svc.get_monitor(session)
