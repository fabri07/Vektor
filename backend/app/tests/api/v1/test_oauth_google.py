"""Tests para /api/v1/auth/oauth/google/

Cubre todos los escenarios definidos en Sprint 2:
  - Feature flag desactivado → 404 en los 4 endpoints
  - start: genera authorization_url con state, nonce, PKCE
  - callback: state inválido/expirado → 409 vía redirect con error=...
  - exchange: sesión inválida/expirada → 400
  - Nuevo usuario Google → crea cuenta, devuelve JWT
  - Identidad ya vinculada → login directo
  - Email existe en cuenta local → link_required
  - link-pending: single-use, credenciales incorrectas, usuario inactivo
  - email_verified=False → fail-closed
  - Concurrencia: estado consumido → segundo request falla
"""

import json
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.models.tenant import Subscription, Tenant
from app.persistence.models.user import User
from app.persistence.models.user_auth_identity import UserAuthIdentity
from app.utils.security import hash_password

# ── Helpers ───────────────────────────────────────────────────────────────────

_GOOGLE_SUB = "google-sub-12345"
_GOOGLE_EMAIL = "user@gmail.com"
_GOOGLE_NAME = "Google User"

_VALID_CLAIMS = {
    "sub": _GOOGLE_SUB,
    "email": _GOOGLE_EMAIL,
    "email_verified": True,
    "name": _GOOGLE_NAME,
    "iss": "accounts.google.com",
    "aud": "test-client-id",
    "nonce": "test-nonce",
    "exp": int(datetime.now(UTC).timestamp()) + 3600,
}


def _make_redis_mock() -> AsyncMock:
    """Redis mock con GETDEL real: devuelve el valor seteado y lo elimina."""
    store: dict[str, str] = {}
    mock = AsyncMock()

    async def _set(key: str, value: str, ex: int | None = None) -> None:
        store[key] = value

    async def _getdel(key: str) -> str | None:
        return store.pop(key, None)

    async def _get(key: str) -> str | None:
        return store.get(key)

    mock.set.side_effect = _set
    mock.getdel.side_effect = _getdel
    mock.get.side_effect = _get
    return mock


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def client_with_google(
    db_session: AsyncSession,
) -> AsyncGenerator[tuple[AsyncClient, AsyncMock], None]:
    """Client con ENABLE_GOOGLE_LOGIN=True y Redis mockeado."""
    from app.config.settings import get_settings
    from app.main import create_app, limiter
    from app.persistence.db.redis import get_redis
    from app.persistence.db.session import get_db_session

    limiter._storage.reset()
    app = create_app()

    redis_mock = _make_redis_mock()

    app.dependency_overrides[get_db_session] = lambda: db_session
    app.dependency_overrides[get_redis] = lambda: redis_mock

    with patch.object(get_settings(), "ENABLE_GOOGLE_LOGIN", True), \
         patch.object(get_settings(), "GOOGLE_OAUTH_CLIENT_ID", "test-client-id"), \
         patch.object(get_settings(), "GOOGLE_OAUTH_CLIENT_SECRET", "test-secret"), \
         patch.object(get_settings(), "GOOGLE_OAUTH_REDIRECT_URI", "http://localhost:8000/cb"):
        async with AsyncClient(
            transport=__import__("httpx").ASGITransport(app=app),
            base_url="http://test",
        ) as ac:
            yield ac, redis_mock


@pytest_asyncio.fixture
async def existing_user(db_session: AsyncSession) -> User:
    """Usuario local con cuenta activa."""
    tenant = Tenant(
        tenant_id=uuid.uuid4(),
        legal_name="Local Biz",
        display_name="Local Biz",
        currency="ARS",
        pricing_reference_mode="MEP",
        status="ACTIVE",
    )
    db_session.add(tenant)
    await db_session.flush()

    sub = Subscription(
        tenant_id=tenant.tenant_id,
        plan_code="FREE",
        billing_index_reference="MEP",
        seats_included=1,
        status="ACTIVE",
    )
    db_session.add(sub)

    user = User(
        tenant_id=tenant.tenant_id,
        email=_GOOGLE_EMAIL,
        full_name="Local User",
        password_hash=hash_password("LocalPass1"),
        role_code="OWNER",
        is_active=True,
    )
    db_session.add(user)
    await db_session.commit()
    return user


@pytest_asyncio.fixture
async def inactive_user(db_session: AsyncSession) -> User:
    """Usuario local con cuenta inactiva (email no verificado)."""
    tenant = Tenant(
        tenant_id=uuid.uuid4(),
        legal_name="Inactive Biz",
        display_name="Inactive Biz",
        currency="ARS",
        pricing_reference_mode="MEP",
        status="ACTIVE",
    )
    db_session.add(tenant)
    await db_session.flush()

    sub = Subscription(
        tenant_id=tenant.tenant_id,
        plan_code="FREE",
        billing_index_reference="MEP",
        seats_included=1,
        status="ACTIVE",
    )
    db_session.add(sub)

    user = User(
        tenant_id=tenant.tenant_id,
        email=_GOOGLE_EMAIL,
        full_name="Inactive User",
        password_hash=hash_password("LocalPass1"),
        role_code="OWNER",
        is_active=False,
    )
    db_session.add(user)
    await db_session.commit()
    return user


# ── Feature flag guard ────────────────────────────────────────────────────────

@pytest.mark.asyncio
class TestFeatureFlagGuard:
    """Todos los endpoints devuelven 404 cuando ENABLE_GOOGLE_LOGIN=False."""

    async def test_start_disabled(self, client: AsyncClient) -> None:
        resp = await client.post("/api/v1/auth/oauth/google/start")
        assert resp.status_code == 404

    async def test_callback_disabled(self, client: AsyncClient) -> None:
        resp = await client.get("/api/v1/auth/oauth/google/callback?code=x&state=y")
        assert resp.status_code == 404

    async def test_exchange_disabled(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/api/v1/auth/oauth/google/exchange",
            json={"session_id": "whatever"},
        )
        assert resp.status_code == 404

    async def test_link_pending_disabled(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/api/v1/auth/oauth/google/link-pending",
            json={
                "pending_oauth_session_id": "x",
                "email": "a@b.com",
                "password": "pass",
            },
        )
        assert resp.status_code == 404


# ── Start ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
class TestOAuthStart:
    async def test_returns_authorization_url(
        self, client_with_google: tuple[AsyncClient, AsyncMock]
    ) -> None:
        ac, _ = client_with_google
        resp = await ac.post("/api/v1/auth/oauth/google/start")

        assert resp.status_code == 200
        data = resp.json()
        assert "authorization_url" in data
        url = data["authorization_url"]
        assert "accounts.google.com" in url
        assert "state=" in url
        assert "nonce=" in url
        assert "code_challenge=" in url
        assert "code_challenge_method=S256" in url
        assert "scope=openid" in url

    async def test_state_stored_in_redis(
        self, client_with_google: tuple[AsyncClient, AsyncMock]
    ) -> None:
        ac, redis = client_with_google
        resp = await ac.post("/api/v1/auth/oauth/google/start")
        assert resp.status_code == 200

        # Redis.set debe haber sido llamado con una clave oauth:state:...
        calls = [str(c) for c in redis.set.call_args_list]
        assert any("oauth:state:" in c for c in calls)


# ── Callback: inválido/expirado ───────────────────────────────────────────────

@pytest.mark.asyncio
class TestOAuthCallbackInvalid:
    async def test_invalid_state_redirects_with_error(
        self, client_with_google: tuple[AsyncClient, AsyncMock]
    ) -> None:
        ac, _ = client_with_google
        # No hay state en Redis → redirige con error
        resp = await ac.get(
            "/api/v1/auth/oauth/google/callback?code=somecode&state=nonexistent",
            follow_redirects=False,
        )
        assert resp.status_code == 302
        assert "error=" in resp.headers["location"]

    async def test_google_error_param_redirects_with_error(
        self, client_with_google: tuple[AsyncClient, AsyncMock]
    ) -> None:
        ac, _ = client_with_google
        resp = await ac.get(
            "/api/v1/auth/oauth/google/callback?error=access_denied&state=x",
            follow_redirects=False,
        )
        assert resp.status_code == 302
        assert "error=access_denied" in resp.headers["location"]

    async def test_state_consumed_on_first_use(
        self, client_with_google: tuple[AsyncClient, AsyncMock]
    ) -> None:
        """Un state ya consumido no puede usarse dos veces."""
        ac, redis = client_with_google

        # Simular que el state existe en Redis
        await redis.set("oauth:state:teststate", json.dumps({
            "nonce": "testnonce",
            "code_verifier": "testverifier",
        }))

        # Primer uso: GETDEL consume el state, pero el token exchange falla
        # (no hay httpx real configurado aquí)
        with patch(
            "app.application.services.google_oauth_service._verify_id_token",
            side_effect=Exception("exchange_failed"),
        ):
            resp1 = await ac.get(
                "/api/v1/auth/oauth/google/callback?code=code1&state=teststate",
                follow_redirects=False,
            )

        # Segundo intento con el mismo state → el GETDEL devuelve None → error
        resp2 = await ac.get(
            "/api/v1/auth/oauth/google/callback?code=code2&state=teststate",
            follow_redirects=False,
        )
        assert resp2.status_code == 302
        assert "error=" in resp2.headers["location"]


# ── Exchange: sesión inválida ─────────────────────────────────────────────────

@pytest.mark.asyncio
class TestOAuthExchangeInvalid:
    async def test_invalid_session_id_returns_400(
        self, client_with_google: tuple[AsyncClient, AsyncMock]
    ) -> None:
        ac, _ = client_with_google
        resp = await ac.post(
            "/api/v1/auth/oauth/google/exchange",
            json={"session_id": "nonexistent"},
        )
        assert resp.status_code == 400

    async def test_session_is_single_use(
        self, client_with_google: tuple[AsyncClient, AsyncMock]
    ) -> None:
        ac, redis = client_with_google

        # Simular un exchange session válido
        payload = {
            "type": "auth",
            "payload": {
                "access_token": "tok",
                "refresh_token": "ref",
                "token_type": "bearer",
                "expires_in": 3600,
                "user": {
                    "user_id": str(uuid.uuid4()),
                    "email": "a@b.com",
                    "full_name": "Test",
                    "role_code": "OWNER",
                    "tenant_id": str(uuid.uuid4()),
                },
            },
        }
        await redis.set("oauth:exchange:mysession", json.dumps(payload))

        resp1 = await ac.post(
            "/api/v1/auth/oauth/google/exchange",
            json={"session_id": "mysession"},
        )
        assert resp1.status_code == 200

        # Segundo intento: GETDEL devuelve None
        resp2 = await ac.post(
            "/api/v1/auth/oauth/google/exchange",
            json={"session_id": "mysession"},
        )
        assert resp2.status_code == 400


# ── Nuevo usuario Google ──────────────────────────────────────────────────────

@pytest.mark.asyncio
class TestOAuthNewUser:
    async def test_new_user_gets_jwt(
        self, client_with_google: tuple[AsyncClient, AsyncMock], db_session: AsyncSession
    ) -> None:
        """Google login con email nuevo crea cuenta y devuelve JWT."""
        ac, redis = client_with_google

        # Poblar state en Redis
        state = "validstate123"
        await redis.set(f"oauth:state:{state}", json.dumps({
            "nonce": "testnonce",
            "code_verifier": "testverifier",
        }))

        token_response = MagicMock()
        token_response.json.return_value = {"id_token": "fake.id.token"}
        token_response.raise_for_status = MagicMock()

        with patch(
            "app.application.services.google_oauth_service._verify_id_token",
            new_callable=AsyncMock,
            return_value=_VALID_CLAIMS,
        ), patch(
            "httpx.AsyncClient.post",
            new_callable=AsyncMock,
            return_value=token_response,
        ):
            resp = await ac.get(
                f"/api/v1/auth/oauth/google/callback?code=authcode&state={state}",
                follow_redirects=False,
            )

        assert resp.status_code == 302
        location = resp.headers["location"]
        assert "session_id=" in location
        session_id = location.split("session_id=")[1]

        # Exchange
        exchange_resp = await ac.post(
            "/api/v1/auth/oauth/google/exchange",
            json={"session_id": session_id},
        )
        assert exchange_resp.status_code == 200
        data = exchange_resp.json()
        assert "access_token" in data
        assert data["user"]["email"] == _GOOGLE_EMAIL

    async def test_new_user_is_active(
        self, client_with_google: tuple[AsyncClient, AsyncMock], db_session: AsyncSession
    ) -> None:
        """El usuario nuevo creado vía Google debe estar activo."""
        from sqlalchemy import select as sa_select
        ac, redis = client_with_google

        state = "stateforactive"
        await redis.set(f"oauth:state:{state}", json.dumps({
            "nonce": "nonce2",
            "code_verifier": "verifier2",
        }))

        token_response = MagicMock()
        token_response.json.return_value = {"id_token": "fake.id.token"}
        token_response.raise_for_status = MagicMock()

        with patch(
            "app.application.services.google_oauth_service._verify_id_token",
            new_callable=AsyncMock,
            return_value={**_VALID_CLAIMS, "email": "newuser@gmail.com", "sub": "new-sub-999"},
        ), patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=token_response):
            await ac.get(
                f"/api/v1/auth/oauth/google/callback?code=x&state={state}",
                follow_redirects=False,
            )

        result = await db_session.execute(
            sa_select(User).where(User.email == "newuser@gmail.com")
        )
        user = result.scalar_one_or_none()
        assert user is not None
        assert user.is_active is True


# ── Identidad ya vinculada ────────────────────────────────────────────────────

@pytest.mark.asyncio
class TestOAuthExistingIdentity:
    async def test_existing_identity_login_direct(
        self,
        client_with_google: tuple[AsyncClient, AsyncMock],
        db_session: AsyncSession,
    ) -> None:
        """Si la identidad ya está vinculada, el login es directo."""
        ac, redis = client_with_google

        # Crear usuario + identidad vinculada
        tenant = Tenant(
            tenant_id=uuid.uuid4(),
            legal_name="T",
            display_name="T",
            currency="ARS",
            pricing_reference_mode="MEP",
            status="ACTIVE",
        )
        db_session.add(tenant)
        await db_session.flush()

        user = User(
            tenant_id=tenant.tenant_id,
            email="linked@gmail.com",
            full_name="Linked User",
            password_hash=hash_password("Pass1234"),
            role_code="OWNER",
            is_active=True,
        )
        db_session.add(user)
        await db_session.flush()

        identity = UserAuthIdentity(
            tenant_id=tenant.tenant_id,
            user_id=user.user_id,
            provider="google",
            provider_subject="existing-sub-456",
            provider_email="linked@gmail.com",
        )
        db_session.add(identity)
        await db_session.commit()

        state = "statelinked"
        await redis.set(f"oauth:state:{state}", json.dumps({
            "nonce": "n3",
            "code_verifier": "v3",
        }))

        token_response = MagicMock()
        token_response.json.return_value = {"id_token": "tok"}
        token_response.raise_for_status = MagicMock()

        with patch(
            "app.application.services.google_oauth_service._verify_id_token",
            new_callable=AsyncMock,
            return_value={
                **_VALID_CLAIMS,
                "sub": "existing-sub-456",
                "email": "linked@gmail.com",
            },
        ), patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=token_response):
            resp = await ac.get(
                f"/api/v1/auth/oauth/google/callback?code=x&state={state}",
                follow_redirects=False,
            )

        session_id = resp.headers["location"].split("session_id=")[1]
        exchange_resp = await ac.post(
            "/api/v1/auth/oauth/google/exchange",
            json={"session_id": session_id},
        )
        assert exchange_resp.status_code == 200
        assert exchange_resp.json()["user"]["email"] == "linked@gmail.com"


# ── link_required ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
class TestOAuthLinkRequired:
    async def _trigger_link_required(
        self,
        ac: AsyncClient,
        redis: AsyncMock,
        user: User,
    ) -> str:
        state = f"state-{uuid.uuid4().hex[:8]}"
        await redis.set(f"oauth:state:{state}", json.dumps({
            "nonce": "nonce-lr",
            "code_verifier": "verifier-lr",
        }))
        token_response = MagicMock()
        token_response.json.return_value = {"id_token": "tok"}
        token_response.raise_for_status = MagicMock()

        with patch(
            "app.application.services.google_oauth_service._verify_id_token",
            new_callable=AsyncMock,
            return_value=_VALID_CLAIMS,  # email == _GOOGLE_EMAIL que ya existe
        ), patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=token_response):
            resp = await ac.get(
                f"/api/v1/auth/oauth/google/callback?code=x&state={state}",
                follow_redirects=False,
            )
        session_id = resp.headers["location"].split("session_id=")[1]
        return session_id

    async def test_existing_local_email_returns_link_required(
        self,
        client_with_google: tuple[AsyncClient, AsyncMock],
        existing_user: User,
    ) -> None:
        ac, redis = client_with_google
        session_id = await self._trigger_link_required(ac, redis, existing_user)

        resp = await ac.post(
            "/api/v1/auth/oauth/google/exchange",
            json={"session_id": session_id},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "link_required"
        assert data["email"] == _GOOGLE_EMAIL
        assert data["provider"] == "google"
        assert "pending_oauth_session_id" in data

    async def test_inactive_user_also_returns_link_required(
        self,
        client_with_google: tuple[AsyncClient, AsyncMock],
        inactive_user: User,
    ) -> None:
        """Usuario inactivo → link_required, no login directo."""
        ac, redis = client_with_google
        session_id = await self._trigger_link_required(ac, redis, inactive_user)

        resp = await ac.post(
            "/api/v1/auth/oauth/google/exchange",
            json={"session_id": session_id},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "link_required"

    async def test_link_pending_success(
        self,
        client_with_google: tuple[AsyncClient, AsyncMock],
        existing_user: User,
    ) -> None:
        """link-pending con credenciales correctas vincula identidad y devuelve JWT."""
        ac, redis = client_with_google

        pending_id = "pending-session-ok"
        await redis.set(f"oauth:link:{pending_id}", json.dumps({
            "provider": "google",
            "provider_subject": _GOOGLE_SUB,
            "provider_email": _GOOGLE_EMAIL,
        }))

        resp = await ac.post(
            "/api/v1/auth/oauth/google/link-pending",
            json={
                "pending_oauth_session_id": pending_id,
                "email": _GOOGLE_EMAIL,
                "password": "LocalPass1",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "access_token" in data
        assert data["user"]["email"] == _GOOGLE_EMAIL

    async def test_link_pending_single_use(
        self,
        client_with_google: tuple[AsyncClient, AsyncMock],
        existing_user: User,
    ) -> None:
        """El pending_session_id es single-use."""
        ac, redis = client_with_google

        pending_id = "pending-single-use"
        await redis.set(f"oauth:link:{pending_id}", json.dumps({
            "provider": "google",
            "provider_subject": _GOOGLE_SUB,
            "provider_email": _GOOGLE_EMAIL,
        }))

        body = {
            "pending_oauth_session_id": pending_id,
            "email": _GOOGLE_EMAIL,
            "password": "LocalPass1",
        }

        resp1 = await ac.post("/api/v1/auth/oauth/google/link-pending", json=body)
        assert resp1.status_code == 200

        resp2 = await ac.post("/api/v1/auth/oauth/google/link-pending", json=body)
        assert resp2.status_code == 400

    async def test_link_pending_wrong_password(
        self,
        client_with_google: tuple[AsyncClient, AsyncMock],
        existing_user: User,
    ) -> None:
        ac, redis = client_with_google

        pending_id = "pending-badpass"
        await redis.set(f"oauth:link:{pending_id}", json.dumps({
            "provider": "google",
            "provider_subject": _GOOGLE_SUB,
            "provider_email": _GOOGLE_EMAIL,
        }))

        resp = await ac.post(
            "/api/v1/auth/oauth/google/link-pending",
            json={
                "pending_oauth_session_id": pending_id,
                "email": _GOOGLE_EMAIL,
                "password": "WrongPassword9",
            },
        )
        assert resp.status_code == 401

    async def test_link_pending_inactive_user_fails(
        self,
        client_with_google: tuple[AsyncClient, AsyncMock],
        inactive_user: User,
    ) -> None:
        """Usuario inactivo no puede completar el link aunque tenga contraseña correcta."""
        ac, redis = client_with_google

        pending_id = "pending-inactive"
        await redis.set(f"oauth:link:{pending_id}", json.dumps({
            "provider": "google",
            "provider_subject": _GOOGLE_SUB,
            "provider_email": _GOOGLE_EMAIL,
        }))

        resp = await ac.post(
            "/api/v1/auth/oauth/google/link-pending",
            json={
                "pending_oauth_session_id": pending_id,
                "email": _GOOGLE_EMAIL,
                "password": "LocalPass1",
            },
        )
        assert resp.status_code == 403

    async def test_link_pending_expired_session(
        self,
        client_with_google: tuple[AsyncClient, AsyncMock],
        existing_user: User,
    ) -> None:
        ac, redis = client_with_google

        resp = await ac.post(
            "/api/v1/auth/oauth/google/link-pending",
            json={
                "pending_oauth_session_id": "expired-or-nonexistent",
                "email": _GOOGLE_EMAIL,
                "password": "LocalPass1",
            },
        )
        assert resp.status_code == 400


# ── email_verified=False fail-closed ─────────────────────────────────────────

@pytest.mark.asyncio
class TestOAuthEmailNotVerified:
    async def test_email_not_verified_redirects_with_error(
        self,
        client_with_google: tuple[AsyncClient, AsyncMock],
    ) -> None:
        ac, redis = client_with_google

        state = "state-unverified"
        await redis.set(f"oauth:state:{state}", json.dumps({
            "nonce": "n4",
            "code_verifier": "v4",
        }))

        unverified_claims = {**_VALID_CLAIMS, "email_verified": False}

        token_response = MagicMock()
        token_response.json.return_value = {"id_token": "tok"}
        token_response.raise_for_status = MagicMock()

        with patch(
            "app.application.services.google_oauth_service._verify_id_token",
            new_callable=AsyncMock,
            return_value=unverified_claims,
        ), patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=token_response):
            resp = await ac.get(
                f"/api/v1/auth/oauth/google/callback?code=x&state={state}",
                follow_redirects=False,
            )

        assert resp.status_code == 302
        assert "error=email_not_verified" in resp.headers["location"]
