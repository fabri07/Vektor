"""User management endpoints."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import (
    get_current_tenant,
    get_current_user,
    require_owner_stepup,
    require_role,
    subscription_http_error,
)
from app.application.services import subscription_service
from app.application.services.pin_service import PinService
from app.application.services.team_permissions_service import aplicar_rol, cambiar_rol
from app.domain.subscription import SUBSCRIPTION_ERRORS
from app.persistence.db.redis_client import get_redis
from app.persistence.db.session import get_db_session
from app.persistence.models.tenant import Tenant
from app.persistence.models.user import User
from app.persistence.repositories.user_repository import UserRepository
from app.schemas.common import MessageResponse
from app.schemas.user import (
    CreateUserRequest,
    UpdateMeRequest,
    UpdateUserRequest,
    UserResponse,
)
from app.utils.security import hash_password

router = APIRouter()


@router.get("/me", response_model=UserResponse, summary="Get current user profile")
async def get_me(current_user: User = Depends(get_current_user)) -> User:
    return current_user


# Declarado ANTES de las rutas /{user_id}: FastAPI matchea en orden de registro
# y "me" no parsea como UUID (daría 422 en vez de llegar acá).
@router.patch("/me", response_model=UserResponse, summary="Update own profile")
async def update_me(
    body: UpdateMeRequest,
    # Perfil propio sin campos sensibles (nombre + teléfono): no requiere PIN
    # ni rol — cualquier usuario edita lo suyo. role/email quedan fuera del schema.
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> User:
    if body.full_name is not None:
        current_user.full_name = body.full_name
    # ``phone: null`` explícito borra el número; omitido no toca nada.
    # La normalización (strip → None) ya la hizo el validator del schema.
    if "phone" in body.model_fields_set:
        current_user.phone = body.phone
    repo = UserRepository(session)
    return await repo.save(current_user)


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
    # OWNER-only: alta de usuarios/roles es gestión de cuentas, no "modificar datos".
    # Evita que una sub-cuenta con can_modify_sensitive cree/eleve roles (escalada).
    _: User = Depends(require_owner_stepup),
    session: AsyncSession = Depends(get_db_session),
) -> User:
    repo = UserRepository(session)
    existing = await repo.get_by_email(body.email, tenant.tenant_id)
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A user with that email already exists in this tenant.",
        )
    # El login busca el email en TODOS los negocios y toma el primero
    # (`get_by_email_any_tenant`). Crear acá un email que ya existe en otro
    # negocio deja una cuenta con la que nunca se puede entrar. Hasta que el
    # login permita elegir negocio, se rechaza.
    if await repo.get_by_email_any_tenant(body.email.lower()) is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "EMAIL_IN_USE_OTHER_BUSINESS",
                "message": (
                    "Ese email ya tiene una cuenta en otro negocio. Usá otro email "
                    "para este usuario."
                ),
            },
        )
    # Plazas del plan: bloquear → contar → crear, todo en ESTA transacción
    # (`repo.save` solo hace flush; el commit es el del request, y hasta ahí
    # vive el lock). Contar antes del lock dejaría entrar dos altas simultáneas.
    try:
        subscription = await subscription_service.assert_tenant_can_write(
            session, tenant.tenant_id, origen="user_create"
        )
        await subscription_service.enforce_seat_limit(
            session,
            subscription=subscription,
            count_active_users=lambda: repo.count_active(tenant.tenant_id),
        )
    except SUBSCRIPTION_ERRORS as exc:
        raise subscription_http_error(exc) from exc

    user = User(
        tenant_id=tenant.tenant_id,
        email=body.email.lower(),
        full_name=body.full_name,
        password_hash=hash_password(body.password),
        role_code=body.role_code,
        is_active=True,
        can_modify_sensitive=False,
        pos_permissions={},
    )
    # Mismas reglas que un cambio de rol: un cajero nace sin permisos de caja.
    aplicar_rol(user, body.role_code)
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
    # OWNER-only: edita roles (role_code) sin restricción → debe ser exclusivo del
    # dueño para que una sub-cuenta no se auto-promueva a OWNER.
    actor: User = Depends(require_owner_stepup),
    session: AsyncSession = Depends(get_db_session),
    redis: Redis = Depends(get_redis),
) -> User:
    repo = UserRepository(session)
    user = await repo.get_by_id(user_id, tenant.tenant_id)
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found.")
    if body.full_name is not None:
        user.full_name = body.full_name
    if body.role_code is not None:
        # La transición limpia lo que el rol nuevo no admite (un cajero sin
        # `can_modify_sensitive`, un ex cajero sin permisos de caja) y la audita.
        await cambiar_rol(session, PinService(redis), actor, user, body.role_code)
    return await repo.save(user)


@router.delete(
    "/{user_id}",
    response_model=MessageResponse,
    summary="Deactivate a user",
)
async def delete_user(
    user_id: UUID,
    tenant: Tenant = Depends(get_current_tenant),
    current_user: User = Depends(require_owner_stepup),
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
