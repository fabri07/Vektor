"""WorkspaceConnectService — flujo OAuth para vincular Google Workspace.

Diferente al login OAuth (google_oauth_service.py):
  - Scopes: gmail.readonly + gmail.compose (no solo openid/email/profile)
  - access_type=offline + prompt=consent → obtenemos refresh_token
  - El resultado se guarda en user_google_workspace_connections (no crea/loguea usuario)
  - ws:state:{state} incluye user_id/tenant_id/flow_id para binding fuerte

Flujo:
  1. generate_start(user_id, tenant_id)   → authorization_url
  2. handle_callback(code, state)          → exchange_session_id (guardado en Redis 60s)
  3. complete_exchange(session_id, user)   → upsert UserGoogleWorkspaceConnection

Invariantes de seguridad:
  - ws:state:{state}: GETDEL atómico (single-use, previene replay)
  - ws:exchange:{id}: GETDEL atómico (single-use)
  - complete_exchange valida que state.user_id == JWT user_id (previene binding cruzado)
  - Tokens persistidos cifrados con Fernet (token_cipher)
  - google_account_email obtenido del userinfo endpoint (no del frontend)
"""

from __future__ import annotations

import hashlib
import json
import secrets
import uuid
from base64 import urlsafe_b64encode
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

import httpx
from fastapi import HTTPException, status
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.security.token_cipher import encrypt_token
from app.config.settings import get_settings
from app.integrations.google_workspace.apps import merge_scopes, scopes_for_apps
from app.observability.logger import get_logger
from app.persistence.models.user_google_workspace import UserGoogleWorkspaceConnection

logger = get_logger(__name__)
settings = get_settings()

_GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
_GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"

_WS_STATE_PREFIX = "ws:state:"
_WS_EXCHANGE_PREFIX = "ws:exchange:"
_STATE_TTL_SECONDS = 600    # 10 min para completar el consentimiento
_EXCHANGE_TTL_SECONDS = 300  # 5 min para que el frontend haga el exchange

def _generate_code_verifier() -> str:
    return secrets.token_urlsafe(48)


def _generate_code_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return urlsafe_b64encode(digest).rstrip(b"=").decode()


class WorkspaceConnectService:
    def __init__(
        self,
        session: AsyncSession,
        redis: Redis,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._session = session
        self._redis = redis
        self._http = http_client

    # ── 1. Start ──────────────────────────────────────────────────────────────

    async def generate_start(
        self,
        user_id: uuid.UUID,
        tenant_id: uuid.UUID,
        app_ids: list[str] | None = None,
        login_hint: str | None = None,
    ) -> str:
        """Genera state/PKCE, los guarda en Redis con binding user_id/tenant_id.

        Returns:
            authorization_url — el frontend debe redirigir al usuario a esta URL.
        """
        state = secrets.token_urlsafe(32)
        flow_id = str(uuid.uuid4())
        code_verifier = _generate_code_verifier()
        code_challenge = _generate_code_challenge(code_verifier)
        nonce = secrets.token_urlsafe(32)

        session_data = {
            "user_id": str(user_id),
            "tenant_id": str(tenant_id),
            "flow_id": flow_id,
            "code_verifier": code_verifier,
            "nonce": nonce,
            "app_ids": app_ids or [],
        }
        await self._redis.set(
            f"{_WS_STATE_PREFIX}{state}",
            json.dumps(session_data),
            ex=_STATE_TTL_SECONDS,
        )

        params: dict[str, str] = {
            "client_id": settings.GOOGLE_OAUTH_CLIENT_ID,
            "redirect_uri": settings.GOOGLE_WORKSPACE_REDIRECT_URI,
            "response_type": "code",
            "scope": " ".join(scopes_for_apps(app_ids)),
            "state": state,
            "nonce": nonce,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "access_type": "offline",
            "prompt": "consent",  # forzar consent para obtener refresh_token siempre
            "include_granted_scopes": "true",
        }
        if login_hint:
            params["login_hint"] = login_hint
        url = "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(params)
        logger.info(
            "workspace.connect.start",
            user_id=str(user_id),
            flow_id=flow_id,
            state_prefix=state[:8],
        )
        return url

    # ── 2. Callback ───────────────────────────────────────────────────────────

    async def handle_callback(self, code: str, state: str) -> str:
        """Procesa el redirect de Google. Intercambia code por tokens y guarda en Redis.

        Returns:
            exchange_session_id — string que el frontend pasa a complete_exchange.

        Raises:
            HTTPException 409 — state inválido o expirado
            HTTPException 400 — intercambio de tokens fallido
            HTTPException 502 — Google no disponible
        """
        # GETDEL atómico — single-use
        raw = await self._redis.getdel(f"{_WS_STATE_PREFIX}{state}")
        if raw is None:
            logger.warning("workspace.callback.invalid_state", state_prefix=state[:8])
            raise HTTPException(status.HTTP_409_CONFLICT, "invalid_or_expired_state")

        state_data: dict = json.loads(raw)
        code_verifier: str = state_data["code_verifier"]
        flow_id: str = state_data["flow_id"]

        # Intercambiar code por tokens
        async with self._http_context() as http:
            try:
                token_resp = await http.post(
                    _GOOGLE_TOKEN_URL,
                    data={
                        "code": code,
                        "client_id": settings.GOOGLE_OAUTH_CLIENT_ID,
                        "client_secret": settings.GOOGLE_OAUTH_CLIENT_SECRET,
                        "redirect_uri": settings.GOOGLE_WORKSPACE_REDIRECT_URI,
                        "grant_type": "authorization_code",
                        "code_verifier": code_verifier,
                    },
                )
                token_resp.raise_for_status()
            except httpx.HTTPStatusError as exc:
                logger.warning(
                    "workspace.callback.token_exchange_failed",
                    status=exc.response.status_code,
                    flow_id=flow_id,
                )
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "token_exchange_failed")
            except httpx.HTTPError as exc:
                logger.error("workspace.callback.google_unavailable", error=str(exc))
                raise HTTPException(status.HTTP_502_BAD_GATEWAY, "google_unavailable")

            token_data = token_resp.json()
            access_token: str = token_data["access_token"]
            refresh_token: str | None = token_data.get("refresh_token")
            expires_in: int = token_data.get("expires_in", 3600)
            scope: str = token_data.get("scope", "")

            # Obtener google_account_email desde userinfo
            google_account_email: str | None = None
            try:
                userinfo_resp = await http.get(
                    _GOOGLE_USERINFO_URL,
                    headers={"Authorization": f"Bearer {access_token}"},
                )
                if userinfo_resp.status_code == 200:
                    google_account_email = userinfo_resp.json().get("email")
            except Exception as exc:
                logger.warning("workspace.callback.userinfo_failed", error=str(exc), flow_id=flow_id)

        # Guardar en Redis (TTL corto — el frontend debe hacer exchange rápido)
        exchange_id = secrets.token_urlsafe(32)
        exchange_data = {
            "user_id": state_data["user_id"],
            "tenant_id": state_data["tenant_id"],
            "flow_id": flow_id,
            "access_token": access_token,
            "refresh_token": refresh_token,
            "expires_in": expires_in,
            "scope": scope,
            "google_account_email": google_account_email,
        }
        await self._redis.set(
            f"{_WS_EXCHANGE_PREFIX}{exchange_id}",
            json.dumps(exchange_data),
            ex=_EXCHANGE_TTL_SECONDS,
        )

        logger.info(
            "workspace.callback.success",
            flow_id=flow_id,
            has_refresh_token=(refresh_token is not None),
            google_account_email=google_account_email,
        )
        return exchange_id

    # ── 3. Exchange ───────────────────────────────────────────────────────────

    async def complete_exchange(
        self,
        exchange_session_id: str,
        current_user_id: uuid.UUID,
        current_tenant_id: uuid.UUID,
    ) -> None:
        """GETDEL del exchange. Valida user binding y upsert en DB.

        Raises:
            HTTPException 400 — sesión inválida o expirada
            HTTPException 403 — user_id en la sesión ≠ JWT user_id (binding mismatch)
        """
        raw = await self._redis.getdel(f"{_WS_EXCHANGE_PREFIX}{exchange_session_id}")
        if raw is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid_or_expired_session")

        data: dict = json.loads(raw)

        # Binding check — previene que usuario B complete el exchange iniciado por A
        if data["user_id"] != str(current_user_id):
            logger.warning(
                "workspace.exchange.user_mismatch",
                state_user=data["user_id"],
                jwt_user=str(current_user_id),
                flow_id=data.get("flow_id"),
            )
            raise HTTPException(status.HTTP_403_FORBIDDEN, "state_user_mismatch")

        incoming_scopes: list[str] = [s for s in data["scope"].split() if s]
        access_token_encrypted = encrypt_token(data["access_token"])

        # refresh_token puede faltar en re-consentimiento (Google no lo reenvía siempre)
        refresh_token_encrypted: str | None = None
        if data.get("refresh_token"):
            refresh_token_encrypted = encrypt_token(data["refresh_token"])

        expires_at = datetime.now(UTC) + timedelta(seconds=data["expires_in"])

        # Upsert: si ya existe una conexión (activa o revocada), se reactiva
        result = await self._session.execute(
            select(UserGoogleWorkspaceConnection).where(
                UserGoogleWorkspaceConnection.user_id == current_user_id,
                UserGoogleWorkspaceConnection.tenant_id == current_tenant_id,
            )
        )
        conn = result.scalar_one_or_none()

        if conn is None:
            scopes = merge_scopes(None, incoming_scopes)
            conn = UserGoogleWorkspaceConnection(
                tenant_id=current_tenant_id,
                user_id=current_user_id,
                access_token_encrypted=access_token_encrypted,
                # None cuando Google no entrega refresh_token (re-consentimiento sin revoke).
                # La columna es nullable desde la migración sprint3.
                # token_manager detecta None/"" y fuerza reconnect si necesita refrescar.
                refresh_token_encrypted=refresh_token_encrypted,
                scopes_granted=scopes,
                expires_at=expires_at,
                google_account_email=data.get("google_account_email"),
            )
            self._session.add(conn)
        else:
            scopes = merge_scopes(conn.scopes_granted, incoming_scopes)
            conn.access_token_encrypted = access_token_encrypted
            if refresh_token_encrypted:
                conn.refresh_token_encrypted = refresh_token_encrypted
            conn.scopes_granted = scopes
            conn.expires_at = expires_at
            conn.revoked_at = None  # reactivar si estaba revocada
            conn.last_error_code = None
            conn.last_error_at = None
            conn.google_account_email = data.get("google_account_email")
            conn.updated_at = datetime.now(UTC)

        await self._session.flush()

        logger.info(
            "workspace.exchange.success",
            user_id=str(current_user_id),
            tenant_id=str(current_tenant_id),
            flow_id=data.get("flow_id"),
            scopes_count=len(scopes),
        )

    # ── Helpers ───────────────────────────────────────────────────────────────

    @asynccontextmanager
    async def _http_context(self) -> AsyncGenerator[httpx.AsyncClient, None]:
        if self._http is not None:
            yield self._http
        else:
            async with httpx.AsyncClient(timeout=15.0) as client:
                yield client
