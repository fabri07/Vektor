"""Tests for /api/v1/auth endpoints."""

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
class TestRegister:
    async def test_register_creates_tenant_and_user(self, client: AsyncClient) -> None:
        response = await client.post(
            "/api/v1/auth/register",
            json={
                "email": "owner@test.com",
                "password": "Secure123",
                "full_name": "Test Owner",
                "tenant_name": "Test Kiosco",
                "vertical": "kiosco",
            },
        )
        assert response.status_code == 201
        data = response.json()
        assert "access_token" in data
        assert "refresh_token" in data
        assert data["token_type"] == "bearer"

    async def test_register_invalid_vertical(self, client: AsyncClient) -> None:
        response = await client.post(
            "/api/v1/auth/register",
            json={
                "email": "owner@test.com",
                "password": "Secure123",
                "full_name": "Test Owner",
                "tenant_name": "Test",
                "vertical": "invalid_vertical",
            },
        )
        assert response.status_code == 422

    async def test_register_weak_password(self, client: AsyncClient) -> None:
        response = await client.post(
            "/api/v1/auth/register",
            json={
                "email": "owner@test.com",
                "password": "short",
                "full_name": "Test Owner",
                "tenant_name": "Test",
                "vertical": "kiosco",
            },
        )
        assert response.status_code == 422


@pytest.mark.asyncio
class TestLogin:
    async def test_login_success(self, client: AsyncClient) -> None:
        # Register first
        await client.post(
            "/api/v1/auth/register",
            json={
                "email": "login@test.com",
                "password": "Secure123",
                "full_name": "Login User",
                "tenant_name": "Login Kiosco",
                "vertical": "kiosco",
            },
        )
        response = await client.post(
            "/api/v1/auth/login",
            json={"email": "login@test.com", "password": "Secure123"},
        )
        assert response.status_code == 200
        assert "access_token" in response.json()

    async def test_login_wrong_password(self, client: AsyncClient) -> None:
        response = await client.post(
            "/api/v1/auth/login",
            json={"email": "nonexistent@test.com", "password": "WrongPass1"},
        )
        assert response.status_code == 401

    async def test_protected_endpoint_requires_auth(self, client: AsyncClient) -> None:
        response = await client.get("/api/v1/users/me")
        assert response.status_code == 403  # No bearer token
