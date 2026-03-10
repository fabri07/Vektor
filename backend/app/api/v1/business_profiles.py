"""Business profile endpoints."""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import get_current_tenant, require_role
from app.persistence.db.session import get_db_session
from app.persistence.models.business import BusinessProfile
from app.persistence.models.tenant import Tenant
from app.persistence.models.user import User
from app.schemas.business_profile import (
    BusinessProfileResponse,
    CreateBusinessProfileRequest,
    UpdateBusinessProfileRequest,
)

router = APIRouter()


@router.get(
    "",
    response_model=BusinessProfileResponse,
    summary="Get the business profile for current tenant",
)
async def get_business_profile(
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db_session),
) -> BusinessProfile:
    result = await session.execute(
        select(BusinessProfile).where(BusinessProfile.tenant_id == tenant.id)
    )
    profile = result.scalar_one_or_none()
    if not profile:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Business profile not found. Please create one first.",
        )
    return profile


@router.post(
    "",
    response_model=BusinessProfileResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create business profile (owner only)",
)
async def create_business_profile(
    body: CreateBusinessProfileRequest,
    tenant: Tenant = Depends(get_current_tenant),
    _: User = Depends(require_role("owner")),
    session: AsyncSession = Depends(get_db_session),
) -> BusinessProfile:
    # Check uniqueness
    result = await session.execute(
        select(BusinessProfile).where(BusinessProfile.tenant_id == tenant.id)
    )
    if result.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Business profile already exists for this tenant.",
        )
    profile = BusinessProfile(
        tenant_id=tenant.id,
        **body.model_dump(),
    )
    session.add(profile)
    await session.flush()
    return profile


@router.patch(
    "",
    response_model=BusinessProfileResponse,
    summary="Update business profile (owner/admin only)",
)
async def update_business_profile(
    body: UpdateBusinessProfileRequest,
    tenant: Tenant = Depends(get_current_tenant),
    _: User = Depends(require_role("owner", "admin")),
    session: AsyncSession = Depends(get_db_session),
) -> BusinessProfile:
    result = await session.execute(
        select(BusinessProfile).where(BusinessProfile.tenant_id == tenant.id)
    )
    profile = result.scalar_one_or_none()
    if not profile:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Profile not found.")
    for field, value in body.model_dump(exclude_none=True).items():
        setattr(profile, field, value)
    session.add(profile)
    await session.flush()
    return profile
