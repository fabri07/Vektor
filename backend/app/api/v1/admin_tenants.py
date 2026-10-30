"""
Administración de tenants — SUPERADMIN (panel de plataforma, F2).

GET  /api/v1/admin/tenants                    — listado paginado
GET  /api/v1/admin/tenants/{tenant_id}        — ficha
POST /api/v1/admin/tenants/{tenant_id}/status — cambiar el estado de CUENTA

El `tenant_id` va SIEMPRE en el path: el SUPERADMIN cruza tenants a propósito y
no por el JWT. Estas rutas no llevan el gate de suscripción del tenant propio.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import exigir_contrasena_superadmin, require_role
from app.application.services import tenant_admin_service as svc
from app.persistence.db.session import get_db_session
from app.persistence.models.user import User
from app.schemas.admin_tenants import (
    AdminTenantDetail,
    AdminTenantListResponse,
    TenantStatusChangeRequest,
    TenantStatusChangeResponse,
)

router = APIRouter(dependencies=[Depends(require_role("SUPERADMIN"))])


@router.get("", response_model=AdminTenantListResponse, summary="Listar tenants")
async def list_tenants(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=svc.MAX_PAGE_SIZE),
    q: str | None = Query(default=None, max_length=100),
    account_status: str | None = Query(default=None, max_length=20),
    session: AsyncSession = Depends(get_db_session),
) -> AdminTenantListResponse:
    return await svc.list_tenants(
        session, page=page, page_size=page_size, search=q, account_status=account_status
    )


@router.get("/{tenant_id}", response_model=AdminTenantDetail, summary="Ficha de un tenant")
async def get_tenant(
    tenant_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
) -> AdminTenantDetail:
    return await svc.get_tenant_detail(session, tenant_id)


@router.post(
    "/{tenant_id}/status",
    response_model=TenantStatusChangeResponse,
    summary="Cambiar el estado de cuenta de un tenant (auditado, idempotente)",
)
async def change_status(
    tenant_id: uuid.UUID,
    body: TenantStatusChangeRequest,
    actor: User = Depends(exigir_contrasena_superadmin),
    session: AsyncSession = Depends(get_db_session),
) -> TenantStatusChangeResponse:
    return await svc.change_tenant_status(
        session,
        tenant_id=tenant_id,
        new_status=body.status,
        reason=body.reason,
        actor=actor,
    )
