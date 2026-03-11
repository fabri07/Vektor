"""User management endpoints."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import get_current_tenant, get_current_user, require_role
from app.persistence.db.session import get_db_session
from app.persistence.models.tenant import Tenant
from app.persistence.models.user import User
from app.persistence.repositories.user_repository import UserRepository
from app.schemas.common import MessageResponse
from app.schemas.user import CreateUserRequest, UpdateUserRequest, UserResponse
from app.utils.security import hash_password

router = APIRouter()


@router.get("/me", response_model=UserResponse, summary="Get current user profile")
async def get_me(current_user: User = Depends(get_current_user)) -> User:
    return current_user


@router.get("", response_model=list[UserResponse], summary="List all users in tenant")
async def list_users(
    tenant: Tenant = Depends(get_current_tenant),
    _: User = Depends(require_role("OWNER", "ADMIN")),
    session: AsyncSession = Depends(get_db_session),
) -> list[User]:
    repo = UserRepository(session)
    return await repo.list_by_tenant(tenant.tenant_id)


@router.post(
    "",
    response_model=UserResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Invite a new user to the tenant",
)
async def create_user(
    body: CreateUserRequest,
    tenant: Tenant = Depends(get_current_tenant),
    _: User = Depends(require_role("OWNER", "ADMIN")),
    session: AsyncSession = Depends(get_db_session),
) -> User:
    repo = UserRepository(session)
    existing = await repo.get_by_email(body.email, tenant.tenant_id)
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A user with that email already exists in this tenant.",
        )
    user = User(
        tenant_id=tenant.tenant_id,
        email=body.email.lower(),
        full_name=body.full_name,
        password_hash=hash_password(body.password),
        role_code=body.role_code,
        is_active=True,
    )
    return await repo.save(user)


@router.get("/{user_id}", response_model=UserResponse, summary="Get user by ID")
async def get_user(
    user_id: UUID,
    tenant: Tenant = Depends(get_current_tenant),
    _: User = Depends(require_role("OWNER", "ADMIN")),
    session: AsyncSession = Depends(get_db_session),
) -> User:
    repo = UserRepository(session)
    user = await repo.get_by_id(user_id, tenant.tenant_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found.")
    return user


@router.patch("/{user_id}", response_model=UserResponse, summary="Update user")
async def update_user(
    user_id: UUID,
    body: UpdateUserRequest,
    tenant: Tenant = Depends(get_current_tenant),
    _: User = Depends(require_role("OWNER", "ADMIN")),
    session: AsyncSession = Depends(get_db_session),
) -> User:
    repo = UserRepository(session)
    user = await repo.get_by_id(user_id, tenant.tenant_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found.")
    if body.full_name is not None:
        user.full_name = body.full_name
    if body.role_code is not None:
        user.role_code = body.role_code
    return await repo.save(user)


@router.delete(
    "/{user_id}",
    response_model=MessageResponse,
    summary="Deactivate a user",
)
async def delete_user(
    user_id: UUID,
    tenant: Tenant = Depends(get_current_tenant),
    current_user: User = Depends(require_role("OWNER")),
    session: AsyncSession = Depends(get_db_session),
) -> MessageResponse:
    repo = UserRepository(session)
    user = await repo.get_by_id(user_id, tenant.tenant_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found.")
    if user.user_id == current_user.user_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot deactivate yourself."
        )
    user.is_active = False
    await repo.save(user)
    return MessageResponse(message="User deactivated.")
