from __future__ import annotations

import uuid
from collections.abc import Generator

import pytest
from fastapi import HTTPException, status

from app.config import get_settings
from app.rate_limit import AuthStartRateLimiter
from app.security import require_internal_context


@pytest.fixture(autouse=True)
def settings_env(monkeypatch: pytest.MonkeyPatch) -> Generator[None, None, None]:
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@localhost:5432/vektor")
    monkeypatch.setenv("GOOGLE_MCP_OAUTH_CLIENT_ID", "client-id")
    monkeypatch.setenv("GOOGLE_MCP_OAUTH_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv("GOOGLE_MCP_OAUTH_REDIRECT_URI", "http://localhost:8080/auth/callback")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_internal_context_rejects_when_shared_secret_is_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MCP_SERVER_SHARED_SECRET", raising=False)
    get_settings.cache_clear()

    with pytest.raises(HTTPException) as exc:
        require_internal_context(
            x_tenant_id=str(uuid.uuid4()),
            x_user_id=str(uuid.uuid4()),
            x_mcp_server_secret="attacker-controlled",
        )

    assert exc.value.status_code == status.HTTP_401_UNAUTHORIZED


def test_internal_context_rejects_wrong_shared_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_SERVER_SHARED_SECRET", "expected-secret")
    get_settings.cache_clear()

    with pytest.raises(HTTPException) as exc:
        require_internal_context(
            x_tenant_id=str(uuid.uuid4()),
            x_user_id=str(uuid.uuid4()),
            x_mcp_server_secret="wrong-secret",
        )

    assert exc.value.status_code == status.HTTP_401_UNAUTHORIZED


def test_auth_start_rate_limiter_blocks_repeated_attempts() -> None:
    limiter = AuthStartRateLimiter(max_requests=2, window_seconds=300)
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()

    limiter.check(tenant_id=tenant_id, user_id=user_id)
    limiter.check(tenant_id=tenant_id, user_id=user_id)

    with pytest.raises(HTTPException) as exc:
        limiter.check(tenant_id=tenant_id, user_id=user_id)

    assert exc.value.status_code == status.HTTP_429_TOO_MANY_REQUESTS
    assert exc.value.headers["Retry-After"]
