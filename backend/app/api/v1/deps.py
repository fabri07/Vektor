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
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services import maintenance_lock_service
from app.application.services.pin_service import PinService
from app.observability.logger import bind_request_context
from app.persistence.db.redis_client import get_redis
from app.persistence.db.session import get_db_session
from app.persistence.models.tenant import Tenant
from app.persistence.models.user import User
from app.persistence.repositories.tenant_repository import TenantRepository
from app.persistence.repositories.user_repository import UserRepository
from app.utils.security import decode_access_token

# Código que el frontend reconoce para abrir el modal de PIN y reintentar.
PIN_REQUIRED_CODE = "PIN_REQUIRED"

_bearer = HTTPBearer(auto_error=False)


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    session: AsyncSession = Depends(get_db_session),
) -> User:
    """Decode JWT and return the authenticated user. Raises 401 if invalid."""
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated.",
            headers={"WWW-Authenticate": "Bearer"},
        )
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

    # Bind tenant_id and user_id into structlog context for this request
    bind_request_context(tenant_id=user.tenant_id, user_id=user.user_id)

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


# ── Step-up auth (PIN) ──────────────────────────────────────────────────────────


async def _require_pin_window(current_user: User, redis: Redis) -> None:
    """Lanza 428 PIN_REQUIRED si no hay ventana de PIN vigente. Fail-closed."""
    pin_service = PinService(redis)
    if not await pin_service.is_window_valid(current_user.tenant_id, current_user.user_id):
        raise HTTPException(
            status_code=status.HTTP_428_PRECONDITION_REQUIRED,
            detail=PIN_REQUIRED_CODE,
        )


async def require_modify_access(
    current_user: User = Depends(get_current_user),
    redis: Redis = Depends(get_redis),
) -> User:
    """Gate para editar/borrar datos ya cargados + configuraciones.

    (a) OWNER o sub-cuenta con ``can_modify_sensitive``, si no → 403.
    (b) ventana de PIN vigente, si no → 428 PIN_REQUIRED.
    """
    if not (current_user.role_code == "OWNER" or current_user.can_modify_sensitive):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No tenés permiso para modificar datos. Pedíselo al dueño de la cuenta.",
        )
    await _require_pin_window(current_user, redis)
    return current_user


async def require_owner_stepup(
    current_user: User = Depends(get_current_user),
    redis: Redis = Depends(get_redis),
) -> User:
    """Gate para acciones exclusivas del OWNER (forzar baja con historial,
    reactivar, permisos de equipo, gestión de usuarios).

    (a) rol OWNER estricto, si no → 403.
    (b) ventana de PIN vigente, si no → 428 PIN_REQUIRED.
    """
    if current_user.role_code != "OWNER":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Solo el dueño de la cuenta puede realizar esta acción.",
        )
    await _require_pin_window(current_user, redis)
    return current_user


# ── Mantenimiento (F3-T3 — dedup de productos) ──────────────────────────────────


async def ensure_tenant_not_under_maintenance(
    tenant_id: UUID = Depends(get_current_tenant_id),
    session: AsyncSession = Depends(get_db_session),
) -> None:
    """Guard HTTP 423 (fast-fail de UX) mientras corre la deduplicación del tenant.

    NO es la garantía de exclusión mutua real — esa la da el advisory lock
    transaccional (``maintenance_lock_service.acquire_write_lock_shared``) que
    cada write boundary toma antes de mutar. Este guard solo evita que el
    cliente mande un request que sabemos de entrada que va a esperar/fallar;
    sin el advisory lock quedaría una carrera TOCTOU (un request que ya pasó
    este chequeo pero escribe mientras el dedup fusiona).
    """
    if await maintenance_lock_service.is_locked(session, tenant_id):
        raise HTTPException(
            status_code=status.HTTP_423_LOCKED,
            detail=(
                "La cuenta está en mantenimiento (deduplicación en curso). "
                "Reintentá en unos minutos."
            ),
        )
