"""Tenant endpoints (read + update own tenant)."""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import get_current_tenant, require_role
from app.persistence.db.session import get_db_session
from app.persistence.models.tenant import Tenant
from app.persistence.models.user import User
from app.persistence.repositories.tenant_repository import TenantRepository
from app.schemas.tenant import TenantResponse, TenantUpdateRequest

router = APIRouter()


@router.get("/me", response_model=TenantResponse, summary="Get current tenant")
async def get_my_tenant(
    tenant: Tenant = Depends(get_current_tenant),
) -> Tenant:
    return tenant


@router.patch(
    "/me",
    response_model=TenantResponse,
    summary="Update current tenant (owner/admin only)",
)
async def update_my_tenant(
    body: TenantUpdateRequest,
    tenant: Tenant = Depends(get_current_tenant),
    _: User = Depends(require_role("OWNER", "ADMIN")),
    session: AsyncSession = Depends(get_db_session),
) -> Tenant:
    repo = TenantRepository(session)
    if body.legal_name is not None:
        tenant.legal_name = body.legal_name
    if body.display_name is not None:
        tenant.display_name = body.display_name
    return await repo.save(tenant)
