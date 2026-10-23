# ruff: noqa: E501
"""
Authentication service.

Handles register, login, refresh, password changes, and email verification.
All writes are fail-closed: if any step fails, the transaction rolls back.

Register creates 5 records atomically:
  Tenant → User → Subscription → BusinessProfile → MomentumProfile

When ENABLE_EMAIL_VERIFICATION is True (and DEBUG is False), register sets
is_active=False and sends a verification email before issuing tokens.
Tokens are issued only after POST /auth/verify-email succeeds.
"""

import uuid
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException, status
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.tenant_provisioning import provision_tenant
from app.config.settings import get_settings
from app.domain.verticals import parse_vertical
from app.integrations.email_templates import render_action_email
from app.observability.logger import get_logger
from app.persistence.models.auth_token import EmailVerificationToken, PasswordResetToken
from app.persistence.models.tenant import Tenant
from app.persistence.models.user import User
from app.persistence.repositories.tenant_repository import TenantRepository
from app.persistence.repositories.user_repository import UserRepository
from app.schemas.auth import (
    AuthResponse,
    RegisterRequest,
    RegisterResponse,
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

_VERIFICATION_TOKEN_TTL_HOURS = 24
_RESET_TOKEN_TTL_HOURS = 1
# TTL largo para el link de invitación tras aprobar una solicitud de acceso
# (Task 8): el usuario puede ser aprobado horas después de haber pedido acceso,
# a diferencia del forgot-password que se pide y usa en el momento. No
# ensanchar _RESET_TOKEN_TTL_HOURS — ese sigue en 1 hora.
_INVITE_TOKEN_TTL_HOURS = 72


def _is_demo_auth_blocked(tenant: Tenant) -> bool:
    """Los tenants demo solo se autentican en entornos local/demo, nunca en producción.

    En producción ``DEMO_MODE`` y ``DEBUG`` están en ``False`` (defaults), así que
    cualquier intento de autenticación (login, refresh, verify-email) contra un
    tenant ``is_demo`` queda rechazado.
    """
    return tenant.is_demo and not (settings.DEMO_MODE or settings.DEBUG)


class AuthService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._user_repo = UserRepository(session)
        self._tenant_repo = TenantRepository(session)

    async def register(self, body: RegisterRequest) -> RegisterResponse:
        """
        Atomic registration: Tenant + User + Subscription + BusinessProfile + MomentumProfile.
        Validates email uniqueness globally before writing.
        Fails closed on any error.

        When ENABLE_EMAIL_VERIFICATION is True, user is created with is_active=False
        and a verification email is sent. Tokens are issued only after verification.
        """
        # 1. Validar email único globalmente
        existing_user = await self._user_repo.get_by_email_any_tenant(body.email.lower())
        if existing_user is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="An account with this email already exists.",
            )

        # 2-6. Acuñar la cuenta: Tenant + User + Subscription + BusinessProfile + MomentumProfile
        is_active = not settings.ENABLE_EMAIL_VERIFICATION
        tenant, user = await provision_tenant(
            self._session,
            business_name=body.business_name,
            email=body.email,
            full_name=body.full_name,
            phone=body.phone,
            vertical=parse_vertical(body.vertical_code),
            password_hash=hash_password(body.password),
            is_active=is_active,
        )

        # 7. Si verificación activa: crear token y enviar email
        if settings.ENABLE_EMAIL_VERIFICATION:
            token = EmailVerificationToken(
                user_id=user.user_id,
                expires_at=datetime.now(UTC) + timedelta(hours=_VERIFICATION_TOKEN_TTL_HOURS),
                used=False,
            )
            self._session.add(token)
            await self._session.flush()
            try:
                self._send_verification_email(user.email, str(token.token_id))
            except Exception as exc:
                logger.warning(
                    "auth.register.verification_email_failed",
                    user_id=str(user.user_id),
                    email=user.email,
                    error=str(exc),
                )

        logger.info(
            "auth.register",
            tenant_id=str(tenant.tenant_id),
            user_id=str(user.user_id),
            vertical_code=body.vertical_code,
            email_verification=settings.ENABLE_EMAIL_VERIFICATION,
        )

        return RegisterResponse(
            message=(
                "Te enviamos un email de verificación. Revisá tu bandeja de entrada."
                if settings.ENABLE_EMAIL_VERIFICATION
                else "Cuenta creada. Podés iniciar sesión."
            ),
            email=user.email,
            requires_verification=settings.ENABLE_EMAIL_VERIFICATION,
        )

    async def login(self, email: str, password: str) -> AuthResponse | None:
        user = await self._user_repo.get_by_email_any_tenant(email.lower())
        if user is None or not verify_password(password, user.password_hash):
            logger.warning("auth.login.failed", email=email)
            return None

        if not user.is_active:
            # Distinguish from wrong-credentials: user exists but hasn't verified email
            logger.warning("auth.login.unverified", user_id=str(user.user_id))
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="email_not_verified",
            )

        tenant = await self._tenant_repo.get_by_id(user.tenant_id)
        if tenant is None or tenant.status not in ("ACTIVE", "TRIAL"):
            return None

        if _is_demo_auth_blocked(tenant):
            # Generic 401 (via None) — no revela que la cuenta existe ni que es demo.
            logger.warning("auth.login.demo_blocked", tenant_id=str(tenant.tenant_id))
            return None

        user.last_login_at = datetime.now(UTC)
        await self._user_repo.save(user)

        logger.info(
            "auth.login.success",
            user_id=str(user.user_id),
            tenant_id=str(tenant.tenant_id),
        )
        return self._build_auth_response(user, tenant)

    async def verify_email(self, token_str: str) -> AuthResponse:
        """
        Validate a verification token, activate the user, and return JWT tokens.
        Always returns 400 on any failure (no detail reveals why).
        """
        try:
            token_uuid = uuid.UUID(token_str)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="invalid_or_expired_token",
            ) from None

        result = await self._session.execute(
            select(EmailVerificationToken).where(
                EmailVerificationToken.token_id == token_uuid,
                EmailVerificationToken.used.is_(False),
                EmailVerificationToken.expires_at > datetime.now(UTC),
            )
        )
        token = result.scalar_one_or_none()
        if token is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="invalid_or_expired_token",
            )

        # Load user directly by user_id (no tenant context needed here)
        user_result = await self._session.execute(select(User).where(User.user_id == token.user_id))
        user = user_result.scalar_one_or_none()
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="invalid_or_expired_token",
            )

        tenant = await self._tenant_repo.get_by_id(user.tenant_id)
        if tenant is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="invalid_or_expired_token",
            )

        if _is_demo_auth_blocked(tenant):
            # Rechazo antes de consumir el token/activar: un tenant demo no se
            # autentica en prod. Mismo 400 genérico del flujo (no revela el motivo).
            logger.warning("auth.verify_email.demo_blocked", tenant_id=str(tenant.tenant_id))
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="invalid_or_expired_token",
            )

        # Activate user and consume token atomically
        token.used = True
        user.is_active = True
        await self._session.flush()

        logger.info("auth.email_verified", user_id=str(user.user_id))
        return self._build_auth_response(user, tenant)

    async def resend_verification(self, email: str) -> None:
        """
        Generate a new verification token for an unverified user and resend the email.
        Silent no-op if the user doesn't exist or is already active (avoids email enumeration).
        """
        user = await self._user_repo.get_by_email_any_tenant(email.lower())
        if user is None or user.is_active:
            return

        # Invalidate all existing unused tokens for this user
        await self._session.execute(
            update(EmailVerificationToken)
            .where(
                EmailVerificationToken.user_id == user.user_id,
                EmailVerificationToken.used.is_(False),
            )
            .values(used=True)
        )

        # Create new token
        token = EmailVerificationToken(
            user_id=user.user_id,
            expires_at=datetime.now(UTC) + timedelta(hours=_VERIFICATION_TOKEN_TTL_HOURS),
            used=False,
        )
        self._session.add(token)
        await self._session.flush()

        self._send_verification_email(user.email, str(token.token_id))
        logger.info("auth.verification_resent", user_id=str(user.user_id))

    async def refresh(self, refresh_token: str) -> TokenResponse | None:
        payload = decode_refresh_token(refresh_token)
        if payload is None:
            return None

        user_id_str = payload.get("sub")
        tenant_id_str = payload.get("tenant_id")
        if not user_id_str or not tenant_id_str:
            return None

        user = await self._user_repo.get_by_id(uuid.UUID(user_id_str), uuid.UUID(tenant_id_str))
        if user is None or not user.is_active:
            return None

        tenant = await self._tenant_repo.get_by_id(user.tenant_id)
        if tenant is None:
            return None

        if _is_demo_auth_blocked(tenant):
            # Impide extender una sesión demo emitida antes del deploy del guard.
            logger.warning("auth.refresh.demo_blocked", tenant_id=str(tenant.tenant_id))
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

    async def change_password(self, user: User, current_password: str, new_password: str) -> bool:
        if not verify_password(current_password, user.password_hash):
            return False
        user.password_hash = hash_password(new_password)
        await self._user_repo.save(user)
        logger.info("auth.password_changed", user_id=str(user.user_id))
        return True

    async def _create_password_reset_token(
        self, user_id: uuid.UUID, *, ttl_hours: int
    ) -> PasswordResetToken:
        """Invalida los tokens de reset vigentes del usuario y crea uno nuevo.

        ``ttl_hours`` es paramétrico: el forgot-password usa
        ``_RESET_TOKEN_TTL_HOURS`` (1h); un TTL largo (``_INVITE_TOKEN_TTL_HOURS``)
        queda disponible para el flujo de invitación por aprobación (Task 8),
        que no ejecuta este método con ese valor todavía.
        """
        await self._session.execute(
            update(PasswordResetToken)
            .where(
                PasswordResetToken.user_id == user_id,
                PasswordResetToken.used.is_(False),
            )
            .values(used=True)
        )

        token = PasswordResetToken(
            user_id=user_id,
            expires_at=datetime.now(UTC) + timedelta(hours=ttl_hours),
            used=False,
        )
        self._session.add(token)
        await self._session.flush()
        return token

    async def request_password_reset(self, email: str) -> None:
        """
        Generate a password reset token and email the link.
        Silent no-op if email not found (avoids enumeration).
        """
        user = await self._user_repo.get_by_email_any_tenant(email.lower())
        if user is None:
            return

        token = await self._create_password_reset_token(
            user.user_id, ttl_hours=_RESET_TOKEN_TTL_HOURS
        )
        await self._session.commit()

        try:
            self._send_reset_email(user.email, str(token.token_id))
        except Exception as exc:
            logger.warning(
                "auth.password_reset.email_failed",
                user_id=str(user.user_id),
                email=user.email,
                error=str(exc),
            )

        logger.info("auth.password_reset.requested", user_id=str(user.user_id))

    async def reset_password(self, token_str: str, new_password: str) -> None:
        """
        Consume a valid reset token and update the user's password.
        Raises 400 for invalid, used, or expired tokens.
        """
        try:
            token_uuid = uuid.UUID(token_str)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="token_invalido_o_expirado",
            ) from None

        result = await self._session.execute(
            select(PasswordResetToken).where(
                PasswordResetToken.token_id == token_uuid,
                PasswordResetToken.used.is_(False),
                PasswordResetToken.expires_at > datetime.now(UTC),
            )
        )
        token = result.scalar_one_or_none()
        if token is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="token_invalido_o_expirado",
            )

        user_result = await self._session.execute(select(User).where(User.user_id == token.user_id))
        user = user_result.scalar_one_or_none()
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="token_invalido_o_expirado",
            )

        user.password_hash = hash_password(new_password)
        token.used = True
        await self._session.flush()
        await self._session.commit()

        logger.info("auth.password_reset.done", user_id=str(user.user_id))

    # ── Private helpers ───────────────────────────────────────────────────────

    def _build_auth_response(self, user: User, tenant: Tenant) -> AuthResponse:
        jwt_payload = {
            "sub": str(user.user_id),
            "tenant_id": str(tenant.tenant_id),
            "role_code": user.role_code,
        }
        access_token = create_access_token(jwt_payload)
        refresh_token = create_refresh_token(jwt_payload)
        return AuthResponse(
            access_token=access_token,
            refresh_token=refresh_token,
            token_type="bearer",
            expires_in=settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES * 60,
            user=UserInAuthResponse(
                user_id=user.user_id,
                email=user.email,
                full_name=user.full_name,
                role_code=user.role_code,
                tenant_id=tenant.tenant_id,
                phone=user.phone,
            ),
        )

    def _send_verification_email(self, to_email: str, token_str: str) -> None:
        from app.integrations.smtp import SMTPClient  # noqa: PLC0415

        verify_url = f"{settings.FRONTEND_URL}/verify-email?token={token_str}"

        html_content, plain_text = render_action_email(
            eyebrow="Verificación de email",
            heading="Confirmá tu dirección de email",
            body=(
                "Hacé click en el botón de abajo para verificar tu cuenta y empezar a usar Véktor. "
                f"El link es válido por {_VERIFICATION_TOKEN_TTL_HOURS} horas."
            ),
            cta_label="Verificar mi email →",
            cta_url=verify_url,
            footnote="Si no creaste una cuenta en Véktor, podés ignorar este email.",
        )

        smtp = SMTPClient()
        smtp.send(
            to_email=to_email,
            subject="Verificá tu email — Véktor",
            body_html=html_content,
            body_text=plain_text,
        )

    def _send_reset_email(self, to_email: str, token_str: str) -> None:
        from app.integrations.smtp import SMTPClient  # noqa: PLC0415

        reset_url = f"{settings.FRONTEND_URL}/reset-password?token={token_str}"

        html_content, plain_text = render_action_email(
            eyebrow="Recuperación de contraseña",
            heading="Restablecé tu contraseña",
            body=(
                "Recibiste este email porque solicitaste restablecer tu contraseña en Véktor. "
                f"Hacé click en el botón de abajo para continuar. Este link expira en {_RESET_TOKEN_TTL_HOURS} hora."
            ),
            cta_label="Restablecer contraseña →",
            cta_url=reset_url,
            footnote="Si no solicitaste este cambio, podés ignorar este email. Tu contraseña no cambia.",
        )

        smtp = SMTPClient()
        smtp.send(
            to_email=to_email,
            subject="Restablecé tu contraseña — Véktor",
            body_html=html_content,
            body_text=plain_text,
        )
