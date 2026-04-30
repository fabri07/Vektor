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

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.models.auth_token import PasswordResetToken
from app.persistence.models.user import User
from app.utils.security import verify_password

# ── Helpers ────────────────────────────────────────────────────────────────────

_REGISTER_PAYLOAD = {
    "email": "owner@kiosco.example.com",
    "password": "Secure123",
    "full_name": "Juan Pérez",
    "business_name": "Kiosco El Rápido",
    "vertical_code": "kiosco",
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
    async def test_forgot_password_unknown_email_returns_200(
        self, client: AsyncClient
    ) -> None:
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
