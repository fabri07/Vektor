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

from app.config.settings import get_settings
from app.observability.logger import get_logger
from app.persistence.models.auth_token import EmailVerificationToken
from app.persistence.models.business import BusinessProfile, MomentumProfile
from app.persistence.models.tenant import Subscription, Tenant
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

        # 2. Crear Tenant (status ACTIVE)
        tenant = Tenant(
            legal_name=body.business_name,
            display_name=body.business_name,
            currency="ARS",
            pricing_reference_mode="MEP",
            status="ACTIVE",
        )
        await self._tenant_repo.save(tenant)

        # 3. Crear User — is_active depends on email verification flag
        is_active = not settings.ENABLE_EMAIL_VERIFICATION
        user = User(
            tenant_id=tenant.tenant_id,
            email=body.email.lower(),
            full_name=body.full_name,
            password_hash=hash_password(body.password),
            role_code="OWNER",
            is_active=is_active,
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
            except Exception:
                logger.warning(
                    "auth.register.verification_email_failed",
                    user_id=str(user.user_id),
                    email=user.email,
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
            )

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
        user_result = await self._session.execute(
            select(User).where(User.user_id == token.user_id)
        )
        user = user_result.scalar_one_or_none()
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="invalid_or_expired_token",
            )

        # Activate user and consume token atomically
        token.used = True
        user.is_active = True
        await self._session.flush()

        tenant = await self._tenant_repo.get_by_id(user.tenant_id)
        if tenant is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="invalid_or_expired_token",
            )

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
            ),
        )

    def _send_verification_email(self, to_email: str, token_str: str) -> None:
        from app.integrations.smtp import SMTPClient  # noqa: PLC0415

        verify_url = f"{settings.FRONTEND_URL}/verify-email?token={token_str}"

        html = f"""<!DOCTYPE html>
<html lang="es">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0"></head>
<body style="margin:0;padding:0;background-color:#f9fafb;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background-color:#f9fafb;padding:40px 0;">
    <tr>
      <td align="center">
        <table width="520" cellpadding="0" cellspacing="0" style="background-color:#ffffff;border-radius:12px;overflow:hidden;border:1px solid #e5e7eb;">
          <tr>
            <td style="background-color:#1A1A2E;padding:28px 32px;">
              <p style="margin:0;font-size:22px;font-weight:700;color:#ffffff;letter-spacing:-0.02em;">Véktor</p>
              <p style="margin:4px 0 0 0;font-size:13px;color:rgba(255,255,255,0.5);">Verificación de email</p>
            </td>
          </tr>
          <tr>
            <td style="padding:32px;">
              <p style="margin:0 0 16px 0;font-size:16px;font-weight:600;color:#111827;">Confirmá tu dirección de email</p>
              <p style="margin:0 0 24px 0;font-size:14px;color:#6b7280;line-height:1.6;">
                Hacé click en el botón de abajo para verificar tu cuenta y empezar a usar Véktor.
                El link es válido por {_VERIFICATION_TOKEN_TTL_HOURS} horas.
              </p>
              <a href="{verify_url}"
                 style="display:inline-block;background-color:#2B7FD4;color:#ffffff;font-size:14px;font-weight:600;
                        text-decoration:none;padding:12px 28px;border-radius:8px;letter-spacing:0.01em;">
                Verificar mi email →
              </a>
              <p style="margin:24px 0 0 0;font-size:12px;color:#9ca3af;">
                Si no creaste una cuenta en Véktor, podés ignorar este email.
              </p>
            </td>
          </tr>
          <tr>
            <td style="padding:16px 32px 24px 32px;border-top:1px solid #f3f4f6;">
              <p style="margin:0;font-size:11px;color:#9ca3af;">
                O copiá este link en tu navegador:<br/>
                <span style="color:#6b7280;word-break:break-all;">{verify_url}</span>
              </p>
            </td>
          </tr>
        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""

        plain = (
            f"Verificá tu email en Véktor.\n\n"
            f"Copiá este link en tu navegador:\n{verify_url}\n\n"
            f"El link es válido por {_VERIFICATION_TOKEN_TTL_HOURS} horas.\n"
            f"Si no creaste una cuenta, podés ignorar este email."
        )

        smtp = SMTPClient()
        smtp.send(
            to_email=to_email,
            subject="Verificá tu email — Véktor",
            body_html=html,
            body_text=plain,
        )
