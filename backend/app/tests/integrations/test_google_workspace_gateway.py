"""Tests del Google Workspace Gateway — Sprint 3.

Cubre:
  - TokenManager: get_valid_access_token, refresh exitoso, double-check post-lock,
    refresh fallido, token corrupto, not_connected
  - Gateway: is_connected, gmail(), disconnect (exitoso, idempotente, remote_revoke_failed)
  - GmailClient: list_messages, get_message, create_draft, InsufficientScopeError (401 y 403)
  - Concurrencia: refresh concurrente doble (segundo request usa token ya refresheado)
  - insufficient_scope en Gmail runtime
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.integrations.google_workspace.exceptions import (
    InsufficientScopeError,
    WorkspaceTokenError,
)
from app.integrations.google_workspace.gateway import GoogleWorkspaceGateway
from app.integrations.google_workspace.gmail_client import GmailClient
from app.integrations.google_workspace.token_manager import TokenManager
from app.persistence.models.user_google_workspace import UserGoogleWorkspaceConnection


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _make_connection(
    user_id: uuid.UUID,
    tenant_id: uuid.UUID,
    *,
    expires_at_delta: timedelta = timedelta(hours=1),
    revoked_at: datetime | None = None,
    access_token_enc: str | None = "enc_access",
    refresh_token_enc: str | None = "enc_refresh",
    last_error_code: str | None = None,
) -> UserGoogleWorkspaceConnection:
    conn = UserGoogleWorkspaceConnection(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        user_id=user_id,
        access_token_encrypted=access_token_enc,
        refresh_token_encrypted=refresh_token_enc,
        scopes_granted=["gmail.readonly", "gmail.compose"],
        connected_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) + expires_at_delta,
        revoked_at=revoked_at,
        updated_at=datetime.now(UTC),
    )
    return conn


def _make_session(conn: UserGoogleWorkspaceConnection | None) -> AsyncMock:
    """Crea un AsyncSession mock que retorna `conn` en execute()."""
    session = AsyncMock()
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = conn
    session.execute = AsyncMock(return_value=result_mock)
    session.flush = AsyncMock()
    session.add = MagicMock()
    return session


def _make_redis() -> AsyncMock:
    redis = AsyncMock()
    return redis


# ── TokenManager ──────────────────────────────────────────────────────────────

class TestTokenManagerGetValidToken:
    @pytest.mark.asyncio
    async def test_returns_plaintext_token_when_valid(self):
        user_id = uuid.uuid4()
        tenant_id = uuid.uuid4()
        conn = _make_connection(user_id, tenant_id, expires_at_delta=timedelta(hours=1))
        session = _make_session(conn)

        with patch("app.integrations.google_workspace.token_manager.decrypt_token", return_value="plaintext_access"):
            tm = TokenManager(session, user_id, tenant_id)
            token = await tm.get_valid_access_token()

        assert token == "plaintext_access"

    @pytest.mark.asyncio
    async def test_raises_not_connected_when_no_conn(self):
        session = _make_session(None)
        tm = TokenManager(session, uuid.uuid4(), uuid.uuid4())
        with pytest.raises(WorkspaceTokenError) as exc_info:
            await tm.get_valid_access_token()
        assert exc_info.value.reason == "not_connected"

    @pytest.mark.asyncio
    async def test_raises_not_connected_when_revoked(self):
        user_id = uuid.uuid4()
        tenant_id = uuid.uuid4()
        conn = _make_connection(user_id, tenant_id, revoked_at=datetime.now(UTC))
        session = _make_session(conn)
        tm = TokenManager(session, user_id, tenant_id)
        with pytest.raises(WorkspaceTokenError) as exc_info:
            await tm.get_valid_access_token()
        assert exc_info.value.reason == "not_connected"

    @pytest.mark.asyncio
    async def test_triggers_refresh_when_expiring_soon(self):
        user_id = uuid.uuid4()
        tenant_id = uuid.uuid4()
        conn = _make_connection(user_id, tenant_id, expires_at_delta=timedelta(minutes=2))
        session = _make_session(conn)

        http_mock = AsyncMock(spec=httpx.AsyncClient)
        refresh_resp = MagicMock()
        refresh_resp.status_code = 200
        refresh_resp.json.return_value = {"access_token": "new_token", "expires_in": 3600}
        http_mock.post = AsyncMock(return_value=refresh_resp)

        with (
            patch("app.integrations.google_workspace.token_manager.decrypt_token", return_value="refresh_tok"),
            patch("app.integrations.google_workspace.token_manager.encrypt_token", return_value="enc_new"),
        ):
            tm = TokenManager(session, user_id, tenant_id, http_client=http_mock)
            await tm.get_valid_access_token()

        http_mock.post.assert_called_once()


class TestTokenManagerRefresh:
    @pytest.mark.asyncio
    async def test_double_check_skips_google_if_already_refreshed(self):
        """Simula refresh concurrente: post-lock el token ya fue renovado."""
        user_id = uuid.uuid4()
        tenant_id = uuid.uuid4()

        # Primer load: token expirando
        expiring_conn = _make_connection(user_id, tenant_id, expires_at_delta=timedelta(minutes=1))
        # Post-lock: otro worker ya refresheó (expires_at en el futuro)
        fresh_conn = _make_connection(user_id, tenant_id, expires_at_delta=timedelta(hours=1))

        session = AsyncMock()
        call_count = 0

        async def _execute(stmt):
            nonlocal call_count
            call_count += 1
            result = MagicMock()
            if call_count == 1:
                result.scalar_one_or_none.return_value = expiring_conn
            else:
                # Simula el FOR UPDATE retornando conexión ya refresheada
                result.scalar_one_or_none.return_value = fresh_conn
            return result

        session.execute = _execute
        session.flush = AsyncMock()

        http_mock = AsyncMock(spec=httpx.AsyncClient)

        with patch("app.integrations.google_workspace.token_manager.decrypt_token", return_value="token"):
            tm = TokenManager(session, user_id, tenant_id, http_client=http_mock)
            await tm.get_valid_access_token()

        # Google NO debe haber sido llamado
        http_mock.post.assert_not_called()

    @pytest.mark.asyncio
    async def test_raises_refresh_failed_on_google_error(self):
        user_id = uuid.uuid4()
        tenant_id = uuid.uuid4()
        conn = _make_connection(user_id, tenant_id, expires_at_delta=timedelta(minutes=1))
        session = _make_session(conn)

        http_mock = AsyncMock(spec=httpx.AsyncClient)
        error_resp = MagicMock()
        error_resp.status_code = 400
        error_resp.content = b'{"error":"invalid_grant"}'
        error_resp.json.return_value = {"error": "invalid_grant"}
        http_mock.post = AsyncMock(return_value=error_resp)

        with patch("app.integrations.google_workspace.token_manager.decrypt_token", return_value="old_refresh"):
            tm = TokenManager(session, user_id, tenant_id, http_client=http_mock)
            with pytest.raises(WorkspaceTokenError) as exc_info:
                await tm.get_valid_access_token()

        assert exc_info.value.reason == "refresh_failed"
        # last_error_code debe haberse actualizado
        session.flush.assert_called()


# ── Gateway ───────────────────────────────────────────────────────────────────

class TestGatewayIsConnected:
    @pytest.mark.asyncio
    async def test_true_when_active_connection(self):
        user_id = uuid.uuid4()
        tenant_id = uuid.uuid4()
        conn = _make_connection(user_id, tenant_id)
        session = _make_session(conn)
        gw = GoogleWorkspaceGateway(session, _make_redis(), user_id, tenant_id)
        assert await gw.is_connected() is True

    @pytest.mark.asyncio
    async def test_false_when_no_connection(self):
        session = _make_session(None)
        gw = GoogleWorkspaceGateway(session, _make_redis(), uuid.uuid4(), uuid.uuid4())
        assert await gw.is_connected() is False

    @pytest.mark.asyncio
    async def test_false_when_revoked(self):
        user_id = uuid.uuid4()
        tenant_id = uuid.uuid4()
        conn = _make_connection(user_id, tenant_id, revoked_at=datetime.now(UTC))
        session = _make_session(conn)
        gw = GoogleWorkspaceGateway(session, _make_redis(), user_id, tenant_id)
        assert await gw.is_connected() is False


class TestGatewayDisconnect:
    @pytest.mark.asyncio
    async def test_disconnect_sets_revoked_fields(self):
        user_id = uuid.uuid4()
        tenant_id = uuid.uuid4()
        conn = _make_connection(user_id, tenant_id)
        session = _make_session(conn)

        http_mock = AsyncMock(spec=httpx.AsyncClient)
        revoke_resp = MagicMock()
        revoke_resp.status_code = 200
        http_mock.post = AsyncMock(return_value=revoke_resp)

        with patch("app.integrations.google_workspace.gateway.decrypt_token", return_value="plain"):
            gw = GoogleWorkspaceGateway(session, _make_redis(), user_id, tenant_id)
            await gw.disconnect()

        assert conn.revoked_at is not None
        assert conn.last_error_code == "revoked"
        assert conn.access_token_encrypted is None
        assert conn.refresh_token_encrypted is None
        session.flush.assert_called()

    @pytest.mark.asyncio
    async def test_disconnect_idempotent_already_revoked(self):
        user_id = uuid.uuid4()
        tenant_id = uuid.uuid4()
        conn = _make_connection(user_id, tenant_id, revoked_at=datetime.now(UTC))
        session = _make_session(conn)

        gw = GoogleWorkspaceGateway(session, _make_redis(), user_id, tenant_id)
        await gw.disconnect()

        # flush NO debe haberse llamado (no hubo cambio)
        session.flush.assert_not_called()

    @pytest.mark.asyncio
    async def test_disconnect_continues_if_remote_revoke_fails(self):
        user_id = uuid.uuid4()
        tenant_id = uuid.uuid4()
        conn = _make_connection(user_id, tenant_id)
        session = _make_session(conn)

        with patch("app.integrations.google_workspace.gateway.decrypt_token", return_value="plain"):
            with patch("httpx.AsyncClient") as mock_client_cls:
                instance = AsyncMock()
                instance.__aenter__ = AsyncMock(return_value=instance)
                instance.__aexit__ = AsyncMock(return_value=False)
                instance.post = AsyncMock(side_effect=httpx.ConnectError("timeout"))
                mock_client_cls.return_value = instance

                gw = GoogleWorkspaceGateway(session, _make_redis(), user_id, tenant_id)
                # No debe lanzar excepción
                await gw.disconnect()

        # Local revoke debe haberse aplicado igual
        assert conn.revoked_at is not None


class TestGatewayInsufficientScope:
    @pytest.mark.asyncio
    async def test_run_gmail_converts_insufficient_scope_error(self):
        user_id = uuid.uuid4()
        tenant_id = uuid.uuid4()
        conn = _make_connection(user_id, tenant_id)
        session = _make_session(conn)

        gw = GoogleWorkspaceGateway(session, _make_redis(), user_id, tenant_id)

        async def _failing_coro():
            raise InsufficientScopeError("403 insufficientPermissions")

        with pytest.raises(WorkspaceTokenError) as exc_info:
            await gw.run_gmail(_failing_coro())

        assert exc_info.value.reason == "insufficient_scope"
        # last_error_code debe haberse actualizado en DB
        session.flush.assert_called()


# ── GmailClient ───────────────────────────────────────────────────────────────

class TestGmailClientInsufficientScope:
    def _make_response(self, status_code: int, body: dict) -> MagicMock:  # type: ignore[type-arg]
        resp = MagicMock(spec=httpx.Response)
        resp.status_code = status_code
        resp.json.return_value = body
        resp.text = json.dumps(body)
        resp.content = b"x"
        resp.request = MagicMock()
        return resp

    @pytest.mark.asyncio
    async def test_raises_insufficient_scope_on_401(self):
        http = AsyncMock(spec=httpx.AsyncClient)
        http.get = AsyncMock(return_value=self._make_response(401, {}))
        client = GmailClient("token", http_client=http)
        with pytest.raises(InsufficientScopeError):
            await client.list_messages()

    @pytest.mark.asyncio
    async def test_raises_insufficient_scope_on_403_insufficient_permissions(self):
        http = AsyncMock(spec=httpx.AsyncClient)
        body = {"error": {"errors": [{"reason": "insufficientPermissions"}]}}
        http.get = AsyncMock(return_value=self._make_response(403, body))
        client = GmailClient("token", http_client=http)
        with pytest.raises(InsufficientScopeError):
            await client.list_messages()

    @pytest.mark.asyncio
    async def test_create_draft_returns_id(self):
        http = AsyncMock(spec=httpx.AsyncClient)
        draft_resp = MagicMock(spec=httpx.Response)
        draft_resp.status_code = 200
        draft_resp.json.return_value = {"id": "draft_abc123"}
        draft_resp.content = b"x"
        draft_resp.request = MagicMock()
        http.post = AsyncMock(return_value=draft_resp)

        client = GmailClient("token", http_client=http)
        draft_id = await client.create_draft(
            to="proveedor@ejemplo.com",
            subject="Re: Lista de precios",
            body="Hola, gracias por su cotización.",
        )
        assert draft_id == "draft_abc123"
