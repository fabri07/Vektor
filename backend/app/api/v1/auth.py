"""Auth endpoints: register, login, me, refresh, logout, change-password,
verify-email, resend-verification."""

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import get_current_user
from app.application.services.auth_service import AuthService
from app.main import limiter
from app.persistence.db.session import get_db_session
from app.persistence.models.business import BusinessProfile
from app.persistence.models.user import User
from app.persistence.repositories.tenant_repository import TenantRepository
from app.schemas.auth import (
    AuthResponse,
    ChangePasswordRequest,
    LoginRequest,
    MeResponse,
    RefreshRequest,
    RegisterRequest,
    RegisterResponse,
    ResendVerificationRequest,
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
    summary="Register a new tenant and owner user",
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
    return MessageResponse(message="Si el email está registrado y pendiente de verificación, recibirás un nuevo link.")


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
) -> MessageResponse:
    # JWT is stateless; logout is handled on the client.
    # Implement token blacklist with Redis if needed.
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
) -> MessageResponse:
    service = AuthService(session)
    ok = await service.change_password(current_user, body.current_password, body.new_password)
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Current password is incorrect.",
        )
    return MessageResponse(message="Password changed successfully.")
