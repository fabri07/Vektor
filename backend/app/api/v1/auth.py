"""Auth endpoints: register, login, me, refresh, logout, change-password,
verify-email, resend-verification."""

from fastapi import APIRouter, Depends, HTTPException, Request, status
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import get_current_user, require_open_registration
from app.application.services.auth_service import AuthService
from app.application.services.pin_service import PinError, PinService
from app.main import limiter
from app.persistence.db.redis_client import get_redis
from app.persistence.db.session import get_db_session
from app.persistence.models.business import BusinessProfile
from app.persistence.models.user import User
from app.persistence.repositories.tenant_repository import TenantRepository
from app.schemas.auth import (
    AuthResponse,
    ChangePasswordRequest,
    ForgotPasswordRequest,
    LoginRequest,
    MeResponse,
    PinChangeRequest,
    PinResetRequest,
    PinSetupRequest,
    PinStatusResponse,
    PinVerifyRequest,
    RefreshRequest,
    RegisterRequest,
    RegisterResponse,
    ResendVerificationRequest,
    ResetPasswordRequest,
    SubscriptionInMeResponse,
    TokenResponse,
    VerifyEmailRequest,
)
from app.schemas.common import MessageResponse

router = APIRouter()


@router.post(
    "/register",
    response_model=RegisterResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Register a new tenant and owner user (410 si el registro abierto está cerrado)",
    # El registro abierto está APAGADO por defecto: el alta pasa por
    # `POST /access-requests` (solicitud + aprobación manual). La ruta se conserva
    # para que un bundle viejo del frontend reciba un 410 accionable en vez de un
    # 404. Prender `ENABLE_OPEN_REGISTRATION` restituye este endpoint tal cual.
    dependencies=[Depends(require_open_registration)],
)
@limiter.limit("5/10minutes")
async def register(
    request: Request,
    body: RegisterRequest,
    session: AsyncSession = Depends(get_db_session),
) -> RegisterResponse:
    service = AuthService(session)
    return await service.register(body)


@router.post(
    "/login",
    response_model=AuthResponse,
    summary="Authenticate and receive a JWT access token",
)
@limiter.limit("10/5minutes")
async def login(
    request: Request,
    body: LoginRequest,
    session: AsyncSession = Depends(get_db_session),
) -> AuthResponse:
    service = AuthService(session)
    result = await service.login(body.email, body.password)
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password.",
        )
    return result


@router.post(
    "/verify-email",
    response_model=AuthResponse,
    summary="Verify email address and receive a JWT access token",
)
@limiter.limit("10/5minutes")
async def verify_email(
    request: Request,
    body: VerifyEmailRequest,
    session: AsyncSession = Depends(get_db_session),
) -> AuthResponse:
    service = AuthService(session)
    return await service.verify_email(body.token)


@router.post(
    "/resend-verification",
    response_model=MessageResponse,
    summary="Resend the email verification link",
)
@limiter.limit("3/15minutes")
async def resend_verification(
    request: Request,
    body: ResendVerificationRequest,
    session: AsyncSession = Depends(get_db_session),
) -> MessageResponse:
    service = AuthService(session)
    await service.resend_verification(body.email)
    # Always return 200 regardless of whether the email exists (avoid enumeration)
    return MessageResponse(
        message=(
            "Si el email está registrado y pendiente de verificación, " "recibirás un nuevo link."
        )
    )


@router.post(
    "/forgot-password",
    response_model=MessageResponse,
    summary="Request a password reset link",
)
@limiter.limit("3/15minutes")
async def forgot_password(
    request: Request,
    body: ForgotPasswordRequest,
    session: AsyncSession = Depends(get_db_session),
) -> MessageResponse:
    service = AuthService(session)
    await service.request_password_reset(body.email)
    return MessageResponse(message="Si el email existe, recibirás un link en minutos.")


@router.post(
    "/reset-password",
    response_model=MessageResponse,
    summary="Reset password using a valid token",
)
@limiter.limit("5/10minutes")
async def reset_password(
    request: Request,
    body: ResetPasswordRequest,
    session: AsyncSession = Depends(get_db_session),
) -> MessageResponse:
    service = AuthService(session)
    await service.reset_password(body.token, body.new_password)
    return MessageResponse(message="Contraseña actualizada correctamente.")


@router.get(
    "/me",
    response_model=MeResponse,
    summary="Return the authenticated user, subscription and onboarding status",
)
async def get_me(
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> MeResponse:
    # Subscription
    tenant_repo = TenantRepository(session)
    subscription = await tenant_repo.get_active_subscription(current_user.tenant_id)

    # BusinessProfile — onboarding_completed flag
    result = await session.execute(
        select(BusinessProfile).where(BusinessProfile.tenant_id == current_user.tenant_id)
    )
    profile = result.scalar_one_or_none()

    return MeResponse(
        user_id=current_user.user_id,
        email=current_user.email,
        full_name=current_user.full_name,
        role_code=current_user.role_code,
        tenant_id=current_user.tenant_id,
        phone=current_user.phone,
        subscription=(
            SubscriptionInMeResponse(
                plan_code=subscription.plan_code,
                status=subscription.status,
            )
            if subscription
            else None
        ),
        onboarding_completed=profile.onboarding_completed if profile else False,
    )


@router.post(
    "/refresh",
    response_model=TokenResponse,
    summary="Refresh access token using a refresh token",
)
async def refresh_token(
    body: RefreshRequest,
    session: AsyncSession = Depends(get_db_session),
) -> TokenResponse:
    service = AuthService(session)
    result = await service.refresh(body.refresh_token)
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired refresh token.",
        )
    return result


@router.post(
    "/logout",
    response_model=MessageResponse,
    summary="Invalidate current session (client-side token deletion)",
)
async def logout(
    current_user: User = Depends(get_current_user),
    redis: Redis = Depends(get_redis),
) -> MessageResponse:
    # JWT is stateless; logout is handled on the client.
    # Cierra la ventana de PIN para que la próxima sesión la vuelva a pedir.
    await PinService(redis).invalidate_window(current_user.tenant_id, current_user.user_id)
    return MessageResponse(message="Logged out successfully.")


@router.post(
    "/change-password",
    response_model=MessageResponse,
    summary="Change authenticated user's password",
)
async def change_password(
    body: ChangePasswordRequest,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
    redis: Redis = Depends(get_redis),
) -> MessageResponse:
    service = AuthService(session)
    ok = await service.change_password(current_user, body.current_password, body.new_password)
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Current password is incorrect.",
        )
    # Cambiar la contraseña invalida la ventana de PIN vigente.
    await PinService(redis).invalidate_window(current_user.tenant_id, current_user.user_id)
    return MessageResponse(message="Password changed successfully.")


# ── PIN step-up ─────────────────────────────────────────────────────────────────


@router.get(
    "/pin/status",
    response_model=PinStatusResponse,
    summary="PIN setup/verification status for the current user",
)
async def pin_status(
    current_user: User = Depends(get_current_user),
    redis: Redis = Depends(get_redis),
) -> PinStatusResponse:
    data = await PinService(redis).get_status(current_user)
    return PinStatusResponse(**data)


@router.post(
    "/pin/setup",
    response_model=MessageResponse,
    summary="Set the 4-digit PIN for the first time",
)
async def pin_setup(
    body: PinSetupRequest,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
    redis: Redis = Depends(get_redis),
) -> MessageResponse:
    # Solo OWNER o sub-cuentas con permiso pueden configurar un PIN.
    if not (current_user.role_code == "OWNER" or current_user.can_modify_sensitive):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No tenés permiso para configurar un PIN.",
        )
    service = PinService(redis, session)
    try:
        await service.setup_pin(current_user, body.pin, body.pin_confirm)
    except PinError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=exc.detail) from exc
    return MessageResponse(message="PIN configurado correctamente.")


@router.post(
    "/pin/verify",
    response_model=MessageResponse,
    summary="Verify the PIN and open a step-up window",
)
@limiter.limit("10/5minutes")
async def pin_verify(
    request: Request,
    body: PinVerifyRequest,
    current_user: User = Depends(get_current_user),
    redis: Redis = Depends(get_redis),
) -> MessageResponse:
    try:
        await PinService(redis).verify(current_user, body.pin)
    except PinError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=exc.detail) from exc
    return MessageResponse(message="PIN verificado.")


@router.post(
    "/pin/change",
    response_model=MessageResponse,
    summary="Change the PIN (requires the current PIN)",
)
@limiter.limit("10/5minutes")
async def pin_change(
    request: Request,
    body: PinChangeRequest,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
    redis: Redis = Depends(get_redis),
) -> MessageResponse:
    service = PinService(redis, session)
    try:
        await service.change_pin(
            current_user, body.current_pin, body.new_pin, body.new_pin_confirm
        )
    except PinError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=exc.detail) from exc
    return MessageResponse(message="PIN actualizado.")


@router.post(
    "/pin/reset",
    response_model=MessageResponse,
    summary="Reset the PIN using the account password",
)
@limiter.limit("5/15minutes")
async def pin_reset(
    request: Request,
    body: PinResetRequest,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
    redis: Redis = Depends(get_redis),
) -> MessageResponse:
    service = PinService(redis, session)
    try:
        await service.reset_pin(
            current_user, body.password, body.new_pin, body.new_pin_confirm
        )
    except PinError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=exc.detail) from exc
    return MessageResponse(message="PIN restablecido.")
