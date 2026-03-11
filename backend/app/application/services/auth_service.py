"""
Authentication service.

Handles register, login, refresh, and password changes.
All writes are fail-closed: if any step fails, the transaction rolls back.

Register creates 5 records atomically:
  Tenant → User → Subscription → BusinessProfile → MomentumProfile
"""

import uuid
from datetime import UTC, datetime

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import get_settings
from app.observability.logger import get_logger
from app.persistence.models.business import BusinessProfile, MomentumProfile
from app.persistence.models.tenant import Subscription, Tenant
from app.persistence.models.user import User
from app.persistence.repositories.tenant_repository import TenantRepository
from app.persistence.repositories.user_repository import UserRepository
from app.schemas.auth import (
    AuthResponse,
    RegisterRequest,
    TokenResponse,
    UserInAuthResponse,
)
from app.utils.security import (
    create_access_token,
    create_refresh_token,
    decode_refresh_token,
    hash_password,
    verify_password,
)

logger = get_logger(__name__)
settings = get_settings()


class AuthService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._user_repo = UserRepository(session)
        self._tenant_repo = TenantRepository(session)

    async def register(self, body: RegisterRequest) -> AuthResponse:
        """
        Atomic registration: Tenant + User + Subscription + BusinessProfile + MomentumProfile.
        Validates email uniqueness globally before writing.
        Fails closed on any error.
        """
        # 1. Validar email único globalmente
        existing_user = await self._user_repo.get_by_email_any_tenant(body.email.lower())
        if existing_user is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="An account with this email already exists.",
            )

        # 2. Crear Tenant (status ACTIVE)
        tenant = Tenant(
            legal_name=body.business_name,
            display_name=body.business_name,
            currency="ARS",
            pricing_reference_mode="MEP",
            status="ACTIVE",
        )
        await self._tenant_repo.save(tenant)

        # 3. Crear User (role_code OWNER, is_active True)
        user = User(
            tenant_id=tenant.tenant_id,
            email=body.email.lower(),
            full_name=body.full_name,
            password_hash=hash_password(body.password),
            role_code="OWNER",
            is_active=True,
        )
        await self._user_repo.save(user)

        # 4. Crear Subscription (plan FREE)
        subscription = Subscription(
            tenant_id=tenant.tenant_id,
            plan_code="FREE",
            billing_index_reference="MEP",
            seats_included=1,
            status="ACTIVE",
        )
        self._session.add(subscription)
        await self._session.flush()

        # 5. Crear BusinessProfile vacío con vertical_code
        profile = BusinessProfile(
            tenant_id=tenant.tenant_id,
            vertical_code=body.vertical_code,
            data_mode="M0",
            data_confidence="LOW",
            onboarding_completed=False,
            heuristic_profile_version="v1",
        )
        self._session.add(profile)
        await self._session.flush()

        # 6. Crear MomentumProfile vacío
        momentum = MomentumProfile(
            tenant_id=tenant.tenant_id,
            improving_streak_weeks=0,
            milestones_json=[],
            updated_at=datetime.now(UTC),
        )
        self._session.add(momentum)
        await self._session.flush()

        logger.info(
            "auth.register",
            tenant_id=str(tenant.tenant_id),
            user_id=str(user.user_id),
            vertical_code=body.vertical_code,
        )

        return self._build_auth_response(user, tenant)

    async def login(self, email: str, password: str) -> AuthResponse | None:
        user = await self._user_repo.get_by_email_any_tenant(email.lower())
        if user is None or not verify_password(password, user.password_hash):
            logger.warning("auth.login.failed", email=email)
            return None
        if not user.is_active:
            logger.warning("auth.login.inactive", user_id=str(user.user_id))
            return None

        tenant = await self._tenant_repo.get_by_id(user.tenant_id)
        if tenant is None or tenant.status not in ("ACTIVE", "TRIAL"):
            return None

        user.last_login_at = datetime.now(UTC)
        await self._user_repo.save(user)

        logger.info(
            "auth.login.success",
            user_id=str(user.user_id),
            tenant_id=str(tenant.tenant_id),
        )
        return self._build_auth_response(user, tenant)

    async def refresh(self, refresh_token: str) -> TokenResponse | None:
        payload = decode_refresh_token(refresh_token)
        if payload is None:
            return None

        user_id_str = payload.get("sub")
        tenant_id_str = payload.get("tenant_id")
        if not user_id_str or not tenant_id_str:
            return None

        user = await self._user_repo.get_by_id(
            uuid.UUID(user_id_str), uuid.UUID(tenant_id_str)
        )
        if user is None or not user.is_active:
            return None

        tenant = await self._tenant_repo.get_by_id(user.tenant_id)
        if tenant is None:
            return None

        jwt_payload = {
            "sub": str(user.user_id),
            "tenant_id": str(tenant.tenant_id),
            "role_code": user.role_code,
        }
        access_token = create_access_token(jwt_payload)
        refresh_token_new = create_refresh_token(jwt_payload)
        return TokenResponse(
            access_token=access_token,
            refresh_token=refresh_token_new,
            token_type="bearer",
            expires_in=settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        )

    async def change_password(
        self, user: User, current_password: str, new_password: str
    ) -> bool:
        if not verify_password(current_password, user.password_hash):
            return False
        user.password_hash = hash_password(new_password)
        await self._user_repo.save(user)
        logger.info("auth.password_changed", user_id=str(user.user_id))
        return True

    def _build_auth_response(self, user: User, tenant: Tenant) -> AuthResponse:
        jwt_payload = {
            "sub": str(user.user_id),
            "tenant_id": str(tenant.tenant_id),
            "role_code": user.role_code,
        }
        access_token = create_access_token(jwt_payload)
        return AuthResponse(
            access_token=access_token,
            token_type="bearer",
            user=UserInAuthResponse(
                user_id=user.user_id,
                email=user.email,
                role_code=user.role_code,
                tenant_id=tenant.tenant_id,
            ),
        )
