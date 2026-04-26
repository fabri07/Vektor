from __future__ import annotations

import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import httpx
import sqlalchemy as sa
from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import GoogleOAuthToken
from app.security import RequestContext, decrypt_secret, encrypt_secret

settings = get_settings()

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_REVOKE_URL = "https://oauth2.googleapis.com/revoke"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"

SHORT_SCOPE_MAP = {
    "gmail.readonly": "https://www.googleapis.com/auth/gmail.readonly",
    "gmail.compose": "https://www.googleapis.com/auth/gmail.compose",
    "gmail.send": "https://www.googleapis.com/auth/gmail.send",
    "calendar.events": "https://www.googleapis.com/auth/calendar.events",
    "spreadsheets": "https://www.googleapis.com/auth/spreadsheets",
    "documents": "https://www.googleapis.com/auth/documents",
    "drive.readonly": "https://www.googleapis.com/auth/drive.readonly",
    "drive.file": "https://www.googleapis.com/auth/drive.file",
}
BASE_IDENTITY_SCOPES = ["openid", "email", "profile"]


def _error_code(value: object, *, fallback: str) -> str:
    raw = str(value or fallback).strip().lower()
    code = "".join(ch if ch.isalnum() or ch in {"_", "-", ":"} else "_" for ch in raw)
    return code[:64] or fallback


def expand_scopes(scopes: list[str]) -> list[str]:
    expanded: list[str] = []
    for scope in [*BASE_IDENTITY_SCOPES, *scopes]:
        expanded.append(SHORT_SCOPE_MAP.get(scope, scope))
    return list(dict.fromkeys(expanded))


async def _get_token_row(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
) -> GoogleOAuthToken | None:
    stmt = sa.select(GoogleOAuthToken).where(
        GoogleOAuthToken.tenant_id == tenant_id,
        GoogleOAuthToken.user_id == user_id,
        GoogleOAuthToken.provider == "google",
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def _get_token_by_state(
    session: AsyncSession,
    state: str,
) -> GoogleOAuthToken | None:
    stmt = sa.select(GoogleOAuthToken).where(GoogleOAuthToken.state_token == state)
    return (await session.execute(stmt)).scalar_one_or_none()


async def start_auth(
    session: AsyncSession,
    ctx: RequestContext,
    scopes: list[str],
) -> dict[str, Any]:
    requested_scopes = expand_scopes(scopes)
    state = secrets.token_urlsafe(32)
    token = await _get_token_row(session, ctx.tenant_id, ctx.user_id)
    if token is None:
        token = GoogleOAuthToken(tenant_id=ctx.tenant_id, user_id=ctx.user_id)
        session.add(token)

    token.state_token = state
    token.state_expires_at = datetime.now(UTC) + timedelta(minutes=10)
    token.scopes_requested = requested_scopes
    token.last_error_code = None
    await session.flush()

    params = {
        "client_id": settings.google_oauth_client_id,
        "redirect_uri": settings.google_oauth_redirect_uri,
        "response_type": "code",
        "access_type": "offline",
        "include_granted_scopes": "true",
        "prompt": "consent",
        "state": state,
        "scope": " ".join(requested_scopes),
    }
    return {
        "auth_url": f"{GOOGLE_AUTH_URL}?{urlencode(params)}",
        "state": state,
        "expires_in": 600,
    }


async def handle_callback(
    session: AsyncSession,
    *,
    state: str,
    code: str,
) -> None:
    token = await _get_token_by_state(session, state)
    if token is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid_state")
    if token.state_expires_at and token.state_expires_at < datetime.now(UTC):
        token.last_error_code = "state_expired"
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="state_expired")

    async with httpx.AsyncClient(timeout=settings.GOOGLE_OAUTH_TIMEOUT_SECONDS) as client:
        resp = await client.post(
            GOOGLE_TOKEN_URL,
            data={
                "code": code,
                "client_id": settings.google_oauth_client_id,
                "client_secret": settings.google_oauth_client_secret,
                "redirect_uri": settings.google_oauth_redirect_uri,
                "grant_type": "authorization_code",
            },
        )
        if resp.status_code >= 400:
            try:
                error_payload = resp.json()
            except ValueError:
                error_payload = {}
            google_error = _error_code(
                error_payload.get("error"),
                fallback=f"http_{resp.status_code}",
            )
            token.last_error_code = f"token_exchange_failed:{google_error}"[:64]
            token.state_token = None
            token.state_expires_at = None
            await session.flush()
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=token.last_error_code,
            )
        payload = resp.json()

        access_token = payload.get("access_token")
        refresh_token = payload.get("refresh_token")
        if not access_token:
            token.last_error_code = "missing_access_token"
            token.state_token = None
            token.state_expires_at = None
            await session.flush()
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="missing_access_token",
            )

        token.access_token_encrypted = encrypt_secret(str(access_token))
        if refresh_token:
            token.refresh_token_encrypted = encrypt_secret(str(refresh_token))
        token.token_type = payload.get("token_type")
        token.scopes_granted = str(payload.get("scope", "")).split()
        expires_in = int(payload.get("expires_in", 3600))
        token.expires_at = datetime.now(UTC) + timedelta(seconds=expires_in - 60)
        token.state_token = None
        token.state_expires_at = None
        token.last_error_code = None

        userinfo = await client.get(
            GOOGLE_USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if userinfo.status_code < 400:
            token.email = userinfo.json().get("email")

        await session.flush()


async def get_status(
    session: AsyncSession,
    ctx: RequestContext,
) -> dict[str, Any]:
    token = await _get_token_row(session, ctx.tenant_id, ctx.user_id)
    if token is None:
        return {"connected": False, "scopes_granted": [], "last_error": "not_connected"}
    if token.last_error_code:
        return {
            "connected": False,
            "scopes_granted": token.scopes_granted or [],
            "last_error": token.last_error_code,
        }
    if token.state_token:
        return {
            "connected": False,
            "scopes_granted": token.scopes_granted or [],
            "last_error": "pending_callback",
        }
    if token.refresh_token_encrypted or token.access_token_encrypted:
        return {
            "connected": True,
            "scopes_granted": token.scopes_granted or [],
            "last_error": token.last_error_code,
            "email": token.email,
        }
    return {
        "connected": False,
        "scopes_granted": [],
        "last_error": token.last_error_code or "not_connected",
    }


async def revoke_auth(
    session: AsyncSession,
    ctx: RequestContext,
) -> None:
    token = await _get_token_row(session, ctx.tenant_id, ctx.user_id)
    if token is None:
        return

    secret = decrypt_secret(token.refresh_token_encrypted) or decrypt_secret(
        token.access_token_encrypted
    )
    if secret:
        async with httpx.AsyncClient(timeout=settings.GOOGLE_OAUTH_TIMEOUT_SECONDS) as client:
            await client.post(
                GOOGLE_REVOKE_URL,
                params={"token": secret},
                headers={"content-type": "application/x-www-form-urlencoded"},
            )

    token.access_token_encrypted = None
    token.refresh_token_encrypted = None
    token.expires_at = None
    token.state_token = None
    token.state_expires_at = None
    token.scopes_requested = []
    token.scopes_granted = []
    token.last_error_code = None
    await session.flush()


async def get_valid_access_token(
    session: AsyncSession,
    ctx: RequestContext,
) -> tuple[str, GoogleOAuthToken]:
    token = await _get_token_row(session, ctx.tenant_id, ctx.user_id)
    if token is None or (not token.access_token_encrypted and not token.refresh_token_encrypted):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="not_connected")

    now = datetime.now(UTC)
    access_token = decrypt_secret(token.access_token_encrypted)
    if access_token and token.expires_at and token.expires_at > now:
        return access_token, token

    refresh_token = decrypt_secret(token.refresh_token_encrypted)
    if not refresh_token:
        token.last_error_code = "refresh_failed"
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="refresh_failed")

    async with httpx.AsyncClient(timeout=settings.GOOGLE_OAUTH_TIMEOUT_SECONDS) as client:
        resp = await client.post(
            GOOGLE_TOKEN_URL,
            data={
                "client_id": settings.google_oauth_client_id,
                "client_secret": settings.google_oauth_client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
        )
        if resp.status_code >= 400:
            token.last_error_code = "refresh_failed"
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="refresh_failed")
        payload = resp.json()
        access_token = str(payload["access_token"])
        token.access_token_encrypted = encrypt_secret(access_token)
        token.token_type = payload.get("token_type", token.token_type)
        expires_in = int(payload.get("expires_in", 3600))
        token.expires_at = datetime.now(UTC) + timedelta(seconds=expires_in - 60)
        if payload.get("scope"):
            token.scopes_granted = str(payload["scope"]).split()
        token.last_error_code = None
        await session.flush()
        return access_token, token
