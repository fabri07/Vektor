from __future__ import annotations

import uuid
from dataclasses import dataclass

from cryptography.fernet import Fernet
from fastapi import Header, HTTPException, status

from app.config import get_settings


@dataclass(frozen=True)
class RequestContext:
    tenant_id: uuid.UUID
    user_id: uuid.UUID


def encrypt_secret(value: str) -> str:
    cipher = Fernet(get_settings().token_cipher_key)
    return cipher.encrypt(value.encode()).decode()


def decrypt_secret(value: str | None) -> str | None:
    if not value:
        return None
    cipher = Fernet(get_settings().token_cipher_key)
    return cipher.decrypt(value.encode()).decode()


def require_internal_context(
    x_tenant_id: str = Header(...),
    x_user_id: str = Header(...),
    x_mcp_server_secret: str | None = Header(default=None),
) -> RequestContext:
    settings = get_settings()
    if settings.MCP_SERVER_SHARED_SECRET and x_mcp_server_secret != settings.MCP_SERVER_SHARED_SECRET:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")

    try:
        return RequestContext(
            tenant_id=uuid.UUID(x_tenant_id),
            user_id=uuid.UUID(x_user_id),
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid_headers") from exc
