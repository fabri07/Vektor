"""
FastAPI dependency injection helpers.

Every business endpoint must inject `get_current_tenant` and
`get_current_user` to enforce authentication and tenant isolation.

JWT payload expected keys: sub (user_id), tenant_id, role_code.
"""

from collections.abc import Callable
from uuid import UUID

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.db.session import get_db_session
from app.persistence.models.tenant import Tenant
from app.persistence.models.user import User
from app.persistence.repositories.tenant_repository import TenantRepository
from app.persistence.repositories.user_repository import UserRepository
from app.utils.security import decode_access_token

_bearer = HTTPBearer()


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(_bearer),
    session: AsyncSession = Depends(get_db_session),
) -> User:
    """Decode JWT and return the authenticated user. Raises 401 if invalid."""
    token = credentials.credentials
    payload = decode_access_token(token)
    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    user_id: str | None = payload.get("sub")
    tenant_id: str | None = payload.get("tenant_id")
    if not user_id or not tenant_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Malformed token.")

    repo = UserRepository(session)
    user = await repo.get_by_id(UUID(user_id), UUID(tenant_id))
    if user is None or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found.")

    return user


def get_current_tenant_id(current_user: User = Depends(get_current_user)) -> UUID:
    """Return the tenant_id of the authenticated user. Propagates to all business queries."""
    return current_user.tenant_id


async def get_current_tenant(
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> Tenant:
    """Return the tenant for the authenticated user. Raises 403 if suspended."""
    repo = TenantRepository(session)
    tenant = await repo.get_by_id(current_user.tenant_id)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found.")
    if tenant.status not in ("ACTIVE", "TRIAL"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Tenant is {tenant.status}.",
        )
    return tenant


def require_role(*roles: str) -> Callable:  # type: ignore[type-arg]
    """Dependency factory that enforces role-based access. Pass uppercase role codes."""

    async def _check(current_user: User = Depends(get_current_user)) -> User:
        if current_user.role_code not in roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Role '{current_user.role_code}' is not allowed to perform this action.",
            )
        return current_user

    return _check
