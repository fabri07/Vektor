"""
Tests for /api/v1/auth endpoints.

Required tests:
  - test_register_success
  - test_register_duplicate_email
  - test_login_success
  - test_login_wrong_password
  - test_me_with_valid_token
  - test_me_with_invalid_token
"""

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services import auth_service
from app.domain.verticals import Vertical
from app.persistence.models.auth_token import EmailVerificationToken, PasswordResetToken
from app.persistence.models.tenant import Tenant
from app.persistence.models.user import User
from app.utils.security import hash_password, verify_password

# ── Helpers ────────────────────────────────────────────────────────────────────

_REGISTER_PAYLOAD = {
    "email": "owner@kiosco.example.com",
    "password": "Secure123",
    "full_name": "Juan Pérez",
    "business_name": "Kiosco El Rápido",
    "vertical_code": Vertical.KIOSCO_ALMACEN.value,
}


# ── Register ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestRegister:
    async def test_register_success(self, client: AsyncClient) -> None:
        """Register creates tenant + user + subscription + business_profile + momentum_profile."""
        response = await client.post("/api/v1/auth/register", json=_REGISTER_PAYLOAD)

        assert response.status_code == 201
        data = response.json()
        assert data["email"] == _REGISTER_PAYLOAD["email"]
        assert "requires_verification" in data
        assert "message" in data

    async def test_register_persists_phone(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        payload = {
            **_REGISTER_PAYLOAD,
            "email": "conphone@kiosco.example.com",
            "phone": "+54 9 11 5555 1234",
        }
        response = await client.post("/api/v1/auth/register", json=payload)
        assert response.status_code == 201
        user = (
            await db_session.execute(select(User).where(User.email == payload["email"]))
        ).scalar_one()
        assert user.phone == "+54 9 11 5555 1234"

    async def test_register_without_phone_is_null(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        payload = {**_REGISTER_PAYLOAD, "email": "sinphone@kiosco.example.com"}
        response = await client.post("/api/v1/auth/register", json=payload)
        assert response.status_code == 201
        user = (
            await db_session.execute(select(User).where(User.email == payload["email"]))
        ).scalar_one()
        assert user.phone is None

    async def test_register_blank_phone_normalizes_to_null(
        self, client: AsyncClient, db_session: AsyncSession
    ) -> None:
        payload = {
            **_REGISTER_PAYLOAD,
            "email": "blankphone@kiosco.example.com",
            "phone": "   ",
        }
        response = await client.post("/api/v1/auth/register", json=payload)
        assert response.status_code == 201
        user = (
            await db_session.execute(select(User).where(User.email == payload["email"]))
        ).scalar_one()
        assert user.phone is None

    async def test_register_duplicate_email(self, client: AsyncClient) -> None:
        """Second register with the same email must return 409."""
        await client.post("/api/v1/auth/register", json=_REGISTER_PAYLOAD)
        response = await client.post("/api/v1/auth/register", json=_REGISTER_PAYLOAD)

        assert response.status_code == 409

    async def test_register_invalid_vertical(self, client: AsyncClient) -> None:
        response = await client.post(
            "/api/v1/auth/register",
            json={**_REGISTER_PAYLOAD, "vertical_code": "farmacia"},
        )
        assert response.status_code == 422

    async def test_register_weak_password(self, client: AsyncClient) -> None:
        response = await client.post(
            "/api/v1/auth/register",
            json={**_REGISTER_PAYLOAD, "password": "short"},
        )
        assert response.status_code == 422


# ── Login ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestLogin:
    async def test_login_success(self, client: AsyncClient) -> None:
        """Register then login — must return access_token and user payload."""
        await client.post("/api/v1/auth/register", json=_REGISTER_PAYLOAD)

        response = await client.post(
            "/api/v1/auth/login",
            json={
                "email": _REGISTER_PAYLOAD["email"],
                "password": _REGISTER_PAYLOAD["password"],
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "access_token" in data
        assert data["token_type"] == "bearer"
        assert data["user"]["email"] == _REGISTER_PAYLOAD["email"]

    async def test_login_wrong_password(self, client: AsyncClient) -> None:
        """Wrong password must return 401."""
        await client.post("/api/v1/auth/register", json=_REGISTER_PAYLOAD)

        response = await client.post(
            "/api/v1/auth/login",
            json={"email": _REGISTER_PAYLOAD["email"], "password": "WrongPass999"},
        )

        assert response.status_code == 401


# ── Me ────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
class TestMe:
    async def test_me_with_valid_token(self, client: AsyncClient) -> None:
        """GET /auth/me with a valid token returns user + subscription + onboarding_completed."""
        await client.post("/api/v1/auth/register", json=_REGISTER_PAYLOAD)
        login = await client.post(
            "/api/v1/auth/login",
            json={"email": _REGISTER_PAYLOAD["email"], "password": _REGISTER_PAYLOAD["password"]},
        )
        assert login.status_code == 200
        token = login.json()["access_token"]

        response = await client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["email"] == _REGISTER_PAYLOAD["email"]
        assert data["role_code"] == "OWNER"
        assert data["onboarding_completed"] is False
        assert data["subscription"]["plan_code"] == "FREE"
        assert data["subscription"]["status"] == "ACTIVE"

    async def test_me_with_invalid_token(self, client: AsyncClient) -> None:
        """GET /auth/me with a garbage token must return 401."""
        response = await client.get(
            "/api/v1/auth/me",
            headers={"Authorization": "Bearer not.a.valid.token"},
        )

        assert response.status_code == 401

    async def test_me_without_token(self, client: AsyncClient) -> None:
        """GET /auth/me without any Authorization header must return 401."""
        response = await client.get("/api/v1/auth/me")
        assert response.status_code == 401


# ── Forgot / Reset password ──────────────────────────────────────────────────


@pytest.mark.asyncio
class TestForgotResetPassword:
    async def test_forgot_password_unknown_email_returns_200(self, client: AsyncClient) -> None:
        response = await client.post(
            "/api/v1/auth/forgot-password",
            json={"email": "missing@example.com"},
        )

        assert response.status_code == 200

    async def test_forgot_password_known_email_creates_token(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        sample_user: User,
    ) -> None:
        with patch("app.integrations.smtp.SMTPClient.send"):
            response = await client.post(
                "/api/v1/auth/forgot-password",
                json={"email": sample_user.email},
            )

        assert response.status_code == 200
        result = await db_session.execute(
            select(PasswordResetToken).where(
                PasswordResetToken.user_id == sample_user.user_id,
                PasswordResetToken.used.is_(False),
            )
        )
        assert result.scalar_one_or_none() is not None

    async def test_reset_password_valid_token(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        sample_user: User,
    ) -> None:
        token = PasswordResetToken(
            user_id=sample_user.user_id,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            used=False,
        )
        db_session.add(token)
        await db_session.commit()

        response = await client.post(
            "/api/v1/auth/reset-password",
            json={"token": str(token.token_id), "new_password": "NewSecure123"},
        )

        assert response.status_code == 200
        await db_session.refresh(sample_user)
        await db_session.refresh(token)
        assert verify_password("NewSecure123", sample_user.password_hash)
        assert token.used is True

    async def test_reset_password_expired_token(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        sample_user: User,
    ) -> None:
        token = PasswordResetToken(
            user_id=sample_user.user_id,
            expires_at=datetime.now(UTC) - timedelta(minutes=1),
            used=False,
        )
        db_session.add(token)
        await db_session.commit()

        response = await client.post(
            "/api/v1/auth/reset-password",
            json={"token": str(token.token_id), "new_password": "NewSecure123"},
        )

        assert response.status_code == 400

    async def test_reset_password_used_token(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        sample_user: User,
    ) -> None:
        token = PasswordResetToken(
            user_id=sample_user.user_id,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            used=True,
        )
        db_session.add(token)
        await db_session.commit()

        response = await client.post(
            "/api/v1/auth/reset-password",
            json={"token": str(token.token_id), "new_password": "NewSecure123"},
        )

        assert response.status_code == 400

    async def test_reset_password_weak_password(self, client: AsyncClient) -> None:
        response = await client.post(
            "/api/v1/auth/reset-password",
            json={"token": "not-a-token", "new_password": "password"},
        )

        assert response.status_code == 422


# ── Demo tenant login guard ───────────────────────────────────────────────────


@pytest.mark.asyncio
class TestDemoTenantLoginGuard:
    """Un tenant is_demo solo puede autenticarse en entornos local/demo, nunca en prod."""

    _EMAIL = "demo.kiosco@vektor.app"
    _PASSWORD = "Demo1234!"

    async def _seed_demo_account(self, db_session: AsyncSession) -> None:
        tenant = Tenant(
            tenant_id=uuid.uuid4(),
            legal_name="Kiosco Demo",
            display_name="Kiosco Demo",
            currency="ARS",
            pricing_reference_mode="MEP",
            status="ACTIVE",
            is_demo=True,
        )
        db_session.add(tenant)
        db_session.add(
            User(
                user_id=uuid.uuid4(),
                tenant_id=tenant.tenant_id,
                email=self._EMAIL,
                full_name="Demo Owner",
                password_hash=hash_password(self._PASSWORD),
                role_code="OWNER",
                is_active=True,
            )
        )
        await db_session.commit()

    async def test_demo_login_blocked_in_prod(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """En producción (DEMO_MODE/DEBUG off) el login demo devuelve 401 genérico."""
        monkeypatch.setattr(auth_service.settings, "DEMO_MODE", False)
        monkeypatch.setattr(auth_service.settings, "DEBUG", False)
        await self._seed_demo_account(db_session)

        response = await client.post(
            "/api/v1/auth/login",
            json={"email": self._EMAIL, "password": self._PASSWORD},
        )

        assert response.status_code == 401

    async def test_demo_login_allowed_in_demo_mode(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Con DEMO_MODE on (dev/local) el demo sigue logueando: 200 + token."""
        monkeypatch.setattr(auth_service.settings, "DEMO_MODE", True)
        monkeypatch.setattr(auth_service.settings, "DEBUG", False)
        await self._seed_demo_account(db_session)

        response = await client.post(
            "/api/v1/auth/login",
            json={"email": self._EMAIL, "password": self._PASSWORD},
        )

        assert response.status_code == 200
        assert response.json()["access_token"]

    async def test_non_demo_login_unaffected_in_prod(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Sanity: un tenant real (is_demo=False) loguea normalmente en prod."""
        monkeypatch.setattr(auth_service.settings, "DEMO_MODE", False)
        monkeypatch.setattr(auth_service.settings, "DEBUG", False)
        tenant = Tenant(
            tenant_id=uuid.uuid4(),
            legal_name="Kiosco Real",
            display_name="Kiosco Real",
            currency="ARS",
            pricing_reference_mode="MEP",
            status="ACTIVE",
            is_demo=False,
        )
        db_session.add(tenant)
        db_session.add(
            User(
                user_id=uuid.uuid4(),
                tenant_id=tenant.tenant_id,
                email="real.owner@kiosco.com",
                full_name="Real Owner",
                password_hash=hash_password("Secure123"),
                role_code="OWNER",
                is_active=True,
            )
        )
        await db_session.commit()

        response = await client.post(
            "/api/v1/auth/login",
            json={"email": "real.owner@kiosco.com", "password": "Secure123"},
        )

        assert response.status_code == 200

    async def test_demo_verify_email_blocked_in_prod(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """verify-email tampoco autentica un tenant demo en prod, y no consume el token."""
        monkeypatch.setattr(auth_service.settings, "DEMO_MODE", False)
        monkeypatch.setattr(auth_service.settings, "DEBUG", False)
        tenant = Tenant(
            tenant_id=uuid.uuid4(),
            legal_name="Kiosco Demo",
            display_name="Kiosco Demo",
            currency="ARS",
            pricing_reference_mode="MEP",
            status="ACTIVE",
            is_demo=True,
        )
        db_session.add(tenant)
        user = User(
            user_id=uuid.uuid4(),
            tenant_id=tenant.tenant_id,
            email="demo.pending@vektor.app",
            full_name="Demo Pending",
            password_hash=hash_password(self._PASSWORD),
            role_code="OWNER",
            is_active=False,
        )
        db_session.add(user)
        token = EmailVerificationToken(
            token_id=uuid.uuid4(),
            user_id=user.user_id,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            used=False,
        )
        db_session.add(token)
        await db_session.commit()

        response = await client.post(
            "/api/v1/auth/verify-email",
            json={"token": str(token.token_id)},
        )

        assert response.status_code == 400
        # El rechazo ocurre antes de mutar: el usuario sigue inactivo y el token sin usar.
        await db_session.refresh(user)
        await db_session.refresh(token)
        assert user.is_active is False
        assert token.used is False
