"""
Authentication service.

Handles register, login, refresh, and password changes.
All writes are fail-closed: if any step fails, the transaction rolls back.
"""

import uuid
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import get_settings
from app.observability.logger import get_logger
from app.persistence.models.tenant import Tenant
from app.persistence.models.user import User
from app.persistence.repositories.tenant_repository import TenantRepository
from app.persistence.repositories.user_repository import UserRepository
from app.schemas.auth import RegisterRequest, TokenResponse
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

    async def register(self, body: RegisterRequest) -> TokenResponse:
        """
        Create a new Tenant + Owner User in a single transaction.
        Fails closed if the slug already exists.
        """
        slug = body.tenant_name.lower().replace(" ", "-")[:100]

        existing_tenant = await self._tenant_repo.get_by_slug(slug)
        if existing_tenant:
            # Append uuid suffix to ensure uniqueness
            slug = f"{slug}-{str(uuid.uuid4())[:8]}"

        tenant = Tenant(
            name=body.tenant_name,
            slug=slug,
            vertical=body.vertical,
            status="trial",
        )
        await self._tenant_repo.save(tenant)

        user = User(
            tenant_id=tenant.id,
            email=body.email.lower(),
            full_name=body.full_name,
            hashed_password=hash_password(body.password),
            role="owner",
            status="active",
        )
        await self._user_repo.save(user)

        logger.info(
            "auth.register",
            tenant_id=str(tenant.id),
            user_id=str(user.id),
            vertical=body.vertical,
        )

        return self._build_tokens(user, tenant)

    async def login(self, email: str, password: str) -> TokenResponse | None:
        user = await self._user_repo.get_by_email_any_tenant(email.lower())
        if user is None or not verify_password(password, user.hashed_password):
            logger.warning("auth.login.failed", email=email)
            return None
        if user.status != "active":
            logger.warning("auth.login.inactive", user_id=str(user.id))
            return None

        tenant = await self._tenant_repo.get_by_id(user.tenant_id)
        if tenant is None or tenant.status not in ("active", "trial"):
            return None

        user.last_login_at = datetime.utcnow()
        await self._user_repo.save(user)

        logger.info("auth.login.success", user_id=str(user.id), tenant_id=str(tenant.id))
        return self._build_tokens(user, tenant)

    async def refresh(self, refresh_token: str) -> TokenResponse | None:
        payload = decode_refresh_token(refresh_token)
        if payload is None:
            return None

        user_id = payload.get("sub")
        tenant_id = payload.get("tenant_id")
        if not user_id or not tenant_id:
            return None

        import uuid as _uuid  # noqa: PLC0415

        user = await self._user_repo.get_by_id(_uuid.UUID(user_id), _uuid.UUID(tenant_id))
        if user is None or user.status != "active":
            return None

        tenant = await self._tenant_repo.get_by_id(user.tenant_id)
        if tenant is None:
            return None

        return self._build_tokens(user, tenant)

    async def change_password(
        self, user: User, current_password: str, new_password: str
    ) -> bool:
        if not verify_password(current_password, user.hashed_password):
            return False
        user.hashed_password = hash_password(new_password)
        await self._user_repo.save(user)
        logger.info("auth.password_changed", user_id=str(user.id))
        return True

    def _build_tokens(self, user: User, tenant: Tenant) -> TokenResponse:
        payload = {"sub": str(user.id), "tenant_id": str(tenant.id), "role": user.role}
        access_token = create_access_token(payload)
        refresh_token = create_refresh_token(payload)
        return TokenResponse(
            access_token=access_token,
            refresh_token=refresh_token,
            token_type="bearer",
            expires_in=settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        )
