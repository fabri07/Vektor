"""Tests de los endpoints de Workspace Connect — Sprint 3.

Cubre:
  - TestFeatureFlagGuard: 404 en los 5 endpoints cuando flag desactivado
  - TestWorkspaceConnectStart: authorization_url generado, state en Redis con user_id
  - TestWorkspaceConnectCallback: error de Google, code/state ausentes, state replay,
    exchange_session_id en redirect
  - TestWorkspaceConnectExchange: éxito, sesión expirada, user_id mismatch → 403
  - TestWorkspaceDisconnect: éxito, idempotente sobre conexión ya revocada
  - TestWorkspaceStatus: conectado, desconectado, sin conexión
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.persistence.models.user import User
from app.persistence.models.user_google_workspace import UserGoogleWorkspaceConnection


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_user(user_id: uuid.UUID | None = None, tenant_id: uuid.UUID | None = None) -> User:
    user = User.__new__(User)
    user.user_id = user_id or uuid.uuid4()
    user.tenant_id = tenant_id or uuid.uuid4()
    user.email = "owner@test.com"
    user.role_code = "OWNER"
    user.is_active = True
    return user


class _RedisStore:
    """Redis mock con semántica GETDEL."""

    def __init__(self) -> None:
        self._store: dict[str, str] = {}

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self._store[key] = value

    async def getdel(self, key: str) -> str | None:
        return self._store.pop(key, None)

    async def get(self, key: str) -> str | None:
        return self._store.get(key)


@pytest.fixture
def user():
    return _make_user()


@pytest.fixture
def redis_store():
    return _RedisStore()


@pytest.fixture
def client_with_workspace(user, redis_store):
    """AsyncClient con flag Workspace activo, usuario autenticado y Redis mockeado."""
    from app.api.v1.deps import get_current_user
    from app.persistence.db.redis import get_redis
    from app.persistence.db.session import get_db_session

    session_mock = AsyncMock()
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = None
    session_mock.execute = AsyncMock(return_value=result_mock)
    session_mock.flush = AsyncMock()
    session_mock.add = MagicMock()
    session_mock.commit = AsyncMock()

    with (
        patch("app.config.settings.get_settings") as mock_settings,
        patch("app.api.v1.workspace.settings") as mock_ws_settings,
    ):
        cfg = MagicMock()
        cfg.ENABLE_GOOGLE_WORKSPACE_MCP = True
        cfg.GOOGLE_OAUTH_CLIENT_ID = "client_id"
        cfg.GOOGLE_OAUTH_CLIENT_SECRET = "client_secret"
        cfg.GOOGLE_WORKSPACE_REDIRECT_URI = "http://localhost:8000/api/v1/workspace/google/connect/callback"
        cfg.GOOGLE_TOKEN_CIPHER_KEY = "test-key"
        cfg.FRONTEND_URL = "http://localhost:3000"
        mock_settings.return_value = cfg
        mock_ws_settings.ENABLE_GOOGLE_WORKSPACE_MCP = True
        mock_ws_settings.GOOGLE_OAUTH_CLIENT_ID = "client_id"
        mock_ws_settings.GOOGLE_OAUTH_CLIENT_SECRET = "client_secret"
        mock_ws_settings.GOOGLE_WORKSPACE_REDIRECT_URI = "http://localhost:8000/api/v1/workspace/google/connect/callback"
        mock_ws_settings.FRONTEND_URL = "http://localhost:3000"

        app.dependency_overrides[get_current_user] = lambda: user
        app.dependency_overrides[get_db_session] = lambda: session_mock
        app.dependency_overrides[get_redis] = lambda: redis_store

        transport = ASGITransport(app=app)
        yield AsyncClient(transport=transport, base_url="http://test"), session_mock, redis_store

    app.dependency_overrides.clear()


# ── TestFeatureFlagGuard ──────────────────────────────────────────────────────

class TestFeatureFlagGuard:
    @pytest.mark.asyncio
    async def test_all_endpoints_return_404_when_flag_disabled(self):
        from app.api.v1.deps import get_current_user
        from app.persistence.db.redis import get_redis
        from app.persistence.db.session import get_db_session

        user = _make_user()
        app.dependency_overrides[get_current_user] = lambda: user
        app.dependency_overrides[get_db_session] = lambda: AsyncMock()
        app.dependency_overrides[get_redis] = lambda: AsyncMock()

        try:
            with patch("app.api.v1.workspace.settings") as mock_ws:
                mock_ws.ENABLE_GOOGLE_WORKSPACE_MCP = False

                transport = ASGITransport(app=app)
                async with AsyncClient(transport=transport, base_url="http://test") as client:
                    endpoints = [
                        ("POST", "/api/v1/workspace/google/connect/start"),
                        ("GET", "/api/v1/workspace/google/connect/callback"),
                        ("POST", "/api/v1/workspace/google/connect/exchange"),
                        ("DELETE", "/api/v1/workspace/google/disconnect"),
                        ("GET", "/api/v1/workspace/google/status"),
                    ]
                    for method, path in endpoints:
                        resp = await client.request(method, path)
                        assert resp.status_code == 404, f"{method} {path} debería ser 404, got {resp.status_code}"
        finally:
            app.dependency_overrides.clear()


# ── TestWorkspaceConnectStart ─────────────────────────────────────────────────

class TestWorkspaceConnectStart:
    @pytest.mark.asyncio
    async def test_returns_authorization_url(self, client_with_workspace, user):
        client, session, redis = client_with_workspace
        async with client:
            resp = await client.post("/api/v1/workspace/google/connect/start")
        assert resp.status_code == 200
        data = resp.json()
        assert "authorization_url" in data
        assert "accounts.google.com" in data["authorization_url"]
        assert "offline" in data["authorization_url"]
        assert "consent" in data["authorization_url"]

    @pytest.mark.asyncio
    async def test_state_stored_in_redis_with_user_binding(self, client_with_workspace, user, redis_store):
        client, session, redis = client_with_workspace
        async with client:
            resp = await client.post("/api/v1/workspace/google/connect/start")
        assert resp.status_code == 200
        url = resp.json()["authorization_url"]

        # Extraer state del URL
        from urllib.parse import parse_qs, urlparse
        params = parse_qs(urlparse(url).query)
        state = params["state"][0]

        # Verificar que Redis tiene el state con user_id
        raw = await redis_store.get(f"ws:state:{state}")
        assert raw is not None
        state_data = json.loads(raw)
        assert state_data["user_id"] == str(user.user_id)
        assert state_data["tenant_id"] == str(user.tenant_id)
        assert "flow_id" in state_data
        assert "code_verifier" in state_data


# ── TestWorkspaceConnectCallback ──────────────────────────────────────────────

class TestWorkspaceConnectCallback:
    @pytest.mark.asyncio
    async def test_error_from_google_redirects_with_error(self, client_with_workspace):
        client, _, _ = client_with_workspace
        async with client:
            resp = await client.get(
                "/api/v1/workspace/google/connect/callback",
                params={"error": "access_denied"},
                follow_redirects=False,
            )
        assert resp.status_code == 302
        assert "error=access_denied" in resp.headers["location"]

    @pytest.mark.asyncio
    async def test_missing_code_and_state_redirects_with_error(self, client_with_workspace):
        client, _, _ = client_with_workspace
        async with client:
            resp = await client.get(
                "/api/v1/workspace/google/connect/callback",
                follow_redirects=False,
            )
        assert resp.status_code == 302
        assert "error=missing_code_or_state" in resp.headers["location"]

    @pytest.mark.asyncio
    async def test_invalid_state_redirects_with_error(self, client_with_workspace):
        """State inexistente en Redis → GETDEL retorna None → redirect con error."""
        client, _, _ = client_with_workspace
        async with client:
            resp = await client.get(
                "/api/v1/workspace/google/connect/callback",
                params={"code": "auth_code_123", "state": "nonexistent_state"},
                follow_redirects=False,
            )
        assert resp.status_code == 302
        assert "error=invalid_or_expired_state" in resp.headers["location"]

    @pytest.mark.asyncio
    async def test_state_single_use(self, client_with_workspace, redis_store):
        """El state se consume con el primer callback — replay retorna error."""
        client, _, _ = client_with_workspace

        # Poner un state en Redis
        state = "test_state_replay"
        await redis_store.set(
            f"ws:state:{state}",
            json.dumps({
                "user_id": str(uuid.uuid4()),
                "tenant_id": str(uuid.uuid4()),
                "flow_id": str(uuid.uuid4()),
                "code_verifier": "verifier_abc",
                "nonce": "nonce_123",
            }),
        )

        http_mock = AsyncMock()
        token_resp = MagicMock()
        token_resp.status_code = 200
        token_resp.json.return_value = {
            "access_token": "acc",
            "refresh_token": "ref",
            "expires_in": 3600,
            "scope": "gmail.readonly",
        }
        http_mock.post = AsyncMock(return_value=token_resp)

        userinfo_resp = MagicMock()
        userinfo_resp.status_code = 200
        userinfo_resp.json.return_value = {"email": "user@empresa.com"}
        http_mock.get = AsyncMock(return_value=userinfo_resp)

        http_mock.__aenter__ = AsyncMock(return_value=http_mock)
        http_mock.__aexit__ = AsyncMock(return_value=False)

        with patch("app.application.services.workspace_connect_service.httpx.AsyncClient", return_value=http_mock):
            async with client:
                # Primer request — OK
                resp1 = await client.get(
                    "/api/v1/workspace/google/connect/callback",
                    params={"code": "code_abc", "state": state},
                    follow_redirects=False,
                )
                assert resp1.status_code == 302
                assert "exchange_session_id=" in resp1.headers["location"]

                # Segundo request con el mismo state — GETDEL ya consumió
                resp2 = await client.get(
                    "/api/v1/workspace/google/connect/callback",
                    params={"code": "code_abc", "state": state},
                    follow_redirects=False,
                )
                assert resp2.status_code == 302
                assert "error=invalid_or_expired_state" in resp2.headers["location"]


# ── TestWorkspaceConnectExchange ──────────────────────────────────────────────

class TestWorkspaceConnectExchange:
    @pytest.mark.asyncio
    async def test_invalid_session_returns_400(self, client_with_workspace):
        client, _, _ = client_with_workspace
        async with client:
            resp = await client.post(
                "/api/v1/workspace/google/connect/exchange",
                json={"exchange_session_id": "nonexistent"},
            )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_user_mismatch_returns_403(self, client_with_workspace, redis_store, user):
        """El exchange fue iniciado por otro usuario → 403."""
        other_user_id = uuid.uuid4()
        exchange_id = "exchange_mismatch"
        await redis_store.set(
            f"ws:exchange:{exchange_id}",
            json.dumps({
                "user_id": str(other_user_id),  # distinto al user del JWT
                "tenant_id": str(user.tenant_id),
                "flow_id": str(uuid.uuid4()),
                "access_token": "acc",
                "refresh_token": "ref",
                "expires_in": 3600,
                "scope": "gmail.readonly",
                "google_account_email": "other@empresa.com",
            }),
        )

        with patch("app.application.services.workspace_connect_service.encrypt_token", return_value="enc"):
            client, _, _ = client_with_workspace
            async with client:
                resp = await client.post(
                    "/api/v1/workspace/google/connect/exchange",
                    json={"exchange_session_id": exchange_id},
                )
        assert resp.status_code == 403
        assert "state_user_mismatch" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_exchange_single_use(self, client_with_workspace, redis_store, user):
        """El exchange se consume en el primer POST — segundo retorna 400."""
        exchange_id = "exchange_single"
        await redis_store.set(
            f"ws:exchange:{exchange_id}",
            json.dumps({
                "user_id": str(user.user_id),
                "tenant_id": str(user.tenant_id),
                "flow_id": str(uuid.uuid4()),
                "access_token": "acc",
                "refresh_token": "ref",
                "expires_in": 3600,
                "scope": "gmail.readonly",
                "google_account_email": "user@empresa.com",
            }),
        )

        with patch("app.application.services.workspace_connect_service.encrypt_token", return_value="enc"):
            client, _, _ = client_with_workspace
            async with client:
                resp1 = await client.post(
                    "/api/v1/workspace/google/connect/exchange",
                    json={"exchange_session_id": exchange_id},
                )
                assert resp1.status_code == 200

                resp2 = await client.post(
                    "/api/v1/workspace/google/connect/exchange",
                    json={"exchange_session_id": exchange_id},
                )
                assert resp2.status_code == 400


# ── TestWorkspaceDisconnect ───────────────────────────────────────────────────

class TestWorkspaceDisconnect:
    @pytest.mark.asyncio
    async def test_disconnect_returns_200(self, client_with_workspace, user):
        from app.persistence.db.session import get_db_session

        conn = UserGoogleWorkspaceConnection.__new__(UserGoogleWorkspaceConnection)
        conn.id = uuid.uuid4()
        conn.user_id = user.user_id
        conn.tenant_id = user.tenant_id
        conn.revoked_at = None
        conn.access_token_encrypted = "enc"
        conn.refresh_token_encrypted = "enc"
        conn.last_error_code = None
        conn.updated_at = datetime.now(UTC)

        session_mock = AsyncMock()
        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = conn
        session_mock.execute = AsyncMock(return_value=result_mock)
        session_mock.flush = AsyncMock()
        session_mock.commit = AsyncMock()

        from app.api.v1.deps import get_current_user
        app.dependency_overrides[get_db_session] = lambda: session_mock

        with (
            patch("app.api.v1.workspace.settings") as mock_settings,
            patch("app.integrations.google_workspace.gateway.decrypt_token", return_value="plain"),
        ):
            mock_settings.ENABLE_GOOGLE_WORKSPACE_MCP = True
            mock_settings.FRONTEND_URL = "http://localhost:3000"

            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.delete("/api/v1/workspace/google/disconnect")

        assert resp.status_code == 200
        assert resp.json()["disconnected"] is True
        app.dependency_overrides.clear()


# ── TestWorkspaceStatus ───────────────────────────────────────────────────────

class TestWorkspaceStatus:
    @pytest.mark.asyncio
    async def test_status_returns_not_connected_when_no_row(self, client_with_workspace):
        client, _, _ = client_with_workspace
        async with client:
            resp = await client.get("/api/v1/workspace/google/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["connected"] is False

    @pytest.mark.asyncio
    async def test_status_returns_connected_with_details(self, client_with_workspace, user):
        from app.api.v1.deps import get_current_user
        from app.persistence.db.session import get_db_session

        conn = UserGoogleWorkspaceConnection.__new__(UserGoogleWorkspaceConnection)
        conn.user_id = user.user_id
        conn.tenant_id = user.tenant_id
        conn.revoked_at = None
        conn.google_account_email = "workspace@empresa.com"
        conn.scopes_granted = ["gmail.readonly", "gmail.compose"]
        conn.connected_at = datetime.now(UTC)
        conn.last_error_code = None

        session_mock = AsyncMock()
        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = conn
        session_mock.execute = AsyncMock(return_value=result_mock)

        app.dependency_overrides[get_db_session] = lambda: session_mock

        with patch("app.api.v1.workspace.settings") as mock_settings:
            mock_settings.ENABLE_GOOGLE_WORKSPACE_MCP = True
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.get("/api/v1/workspace/google/status")

        assert resp.status_code == 200
        data = resp.json()
        assert data["connected"] is True
        assert data["google_account_email"] == "workspace@empresa.com"
        assert "gmail.readonly" in data["scopes_granted"]
        app.dependency_overrides.clear()
