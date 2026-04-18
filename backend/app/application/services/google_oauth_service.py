"""Google OAuth service — Sprint 2: Login social.

Responsabilidades:
  1. generate_start()        → genera state/nonce/PKCE, los guarda en Redis, devuelve auth URL
  2. handle_callback()       → valida state+PKCE, intercambia code, verifica id_token
                               guarda resultado en Redis, devuelve session_id para exchange
  3. exchange_session()      → GETDEL del resultado (single-use), devuelve AuthResponse o LinkRequiredResponse
  4. complete_link()         → GETDEL del pending_oauth_session, verifica password, vincula identidad

Flujo completo:
  POST /start
    → genera params, guarda en Redis oauth:state:{state} (TTL 10 min)
    → devuelve {authorization_url}

  GET /callback (browser redirect desde Google)
    → GETDEL oauth:state:{state}  ← single-use atómico
    → intercambia code, verifica id_token, chequea email_verified + nonce
    → guarda resultado en Redis oauth:exchange:{session_id} (TTL 60 seg)
    → redirige a {FRONTEND_URL}/oauth/callback?session_id={id}

  POST /exchange {session_id}
    → GETDEL oauth:exchange:{session_id}
    → devuelve AuthResponse (nuevo usuario o identidad ya vinculada)
       o OAuthLinkRequiredResponse (email ya existe en cuenta local)

  POST /link-pending {pending_oauth_session_id, email, password}
    → GETDEL oauth:link:{id}  ← single-use atómico
    → autentica con password (fail si is_active=False)
    → vincula UserAuthIdentity
    → devuelve AuthResponse

Invariantes de seguridad:
  - id_token verificado contra Google JWKS (RS256)
  - email_verified=False → fail-closed (400)
  - state/nonce/link sessions: GETDEL atómico (no GET + DEL)
  - Tokens de Google NO se persisten para el flujo de login
  - Usuarios inactivos (is_active=False): responde link_required, no login directo
"""

from __future__ import annotations

import hashlib
import json
import secrets
import uuid
from base64 import urlsafe_b64encode
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode

import httpx
from fastapi import HTTPException, status
from jose import JWTError, jwk, jwt as jose_jwt
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import get_settings
from app.observability.logger import get_logger
from app.persistence.models.business import MomentumProfile
from app.persistence.models.tenant import Subscription, Tenant
from app.persistence.models.user import User
from app.persistence.models.user_auth_identity import UserAuthIdentity
from app.persistence.repositories.tenant_repository import TenantRepository
from app.persistence.repositories.user_repository import UserRepository
from app.schemas.auth import AuthResponse, UserInAuthResponse
from app.schemas.oauth import OAuthLinkRequiredResponse, OAuthStartResponse
from app.utils.security import (
    create_access_token,
    create_refresh_token,
    hash_password,
    verify_password,
)

logger = get_logger(__name__)
settings = get_settings()

# ── Constantes ────────────────────────────────────────────────────────────────
_GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
_GOOGLE_JWKS_URL = "https://www.googleapis.com/oauth2/v3/certs"
_GOOGLE_VALID_ISSUERS = {"accounts.google.com", "https://accounts.google.com"}

_STATE_TTL_SECONDS = 600    # 10 min — tiempo que el usuario tiene para completar el flujo OAuth
_EXCHANGE_TTL_SECONDS = 60  # 60 seg — ventana para que el frontend haga el exchange post-redirect
_LINK_TTL_SECONDS = 600     # 10 min — tiempo para completar el link_required

# Cache en memoria del JWKS de Google (se invalida cada hora)
_jwks_cache: dict[str, Any] | None = None
_jwks_cached_at: datetime | None = None
_JWKS_CACHE_TTL_SECONDS = 3600


# ── PKCE helpers ──────────────────────────────────────────────────────────────

def _generate_code_verifier() -> str:
    """43-128 chars URL-safe base64 sin padding (RFC 7636)."""
    return secrets.token_urlsafe(48)  # 64 chars aprox


def _generate_code_challenge(verifier: str) -> str:
    """S256: BASE64URL(SHA256(verifier))."""
    digest = hashlib.sha256(verifier.encode()).digest()
    return urlsafe_b64encode(digest).rstrip(b"=").decode()


# ── Google JWKS ───────────────────────────────────────────────────────────────

async def _get_google_jwks(http: httpx.AsyncClient) -> dict[str, Any]:
    """Fetch Google's JWKS con cache en memoria de 1 hora."""
    global _jwks_cache, _jwks_cached_at  # noqa: PLW0603

    now = datetime.now(UTC)
    if (
        _jwks_cache is not None
        and _jwks_cached_at is not None
        and (now - _jwks_cached_at).total_seconds() < _JWKS_CACHE_TTL_SECONDS
    ):
        return _jwks_cache

    resp = await http.get(_GOOGLE_JWKS_URL)
    resp.raise_for_status()
    _jwks_cache = resp.json()
    _jwks_cached_at = now
    return _jwks_cache


# ── id_token verification ──────────────────────────────────────────────────────

async def _verify_id_token(
    id_token: str,
    expected_nonce: str,
    http: httpx.AsyncClient,
) -> dict[str, Any]:
    """Verifica el id_token de Google contra JWKS (RS256).

    Raises HTTPException 400 en cualquier fallo de validación.
    """
    try:
        header = jose_jwt.get_unverified_header(id_token)
    except JWTError as exc:
        logger.warning("oauth.id_token.bad_header", error=str(exc))
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid_id_token")

    kid = header.get("kid")
    if not kid:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid_id_token")

    try:
        jwks = await _get_google_jwks(http)
    except httpx.HTTPError as exc:
        logger.error("oauth.jwks.fetch_failed", error=str(exc))
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "google_unavailable")

    key_data = next((k for k in jwks.get("keys", []) if k.get("kid") == kid), None)
    if key_data is None:
        # Invalidar cache y reintentar una vez (rotación de clave)
        global _jwks_cache  # noqa: PLW0603
        _jwks_cache = None
        try:
            jwks = await _get_google_jwks(http)
        except httpx.HTTPError as exc:
            logger.error("oauth.jwks.retry_failed", error=str(exc))
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, "google_unavailable")
        key_data = next((k for k in jwks.get("keys", []) if k.get("kid") == kid), None)

    if key_data is None:
        logger.warning("oauth.id_token.unknown_kid", kid=kid)
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid_id_token")

    try:
        rsa_key = jwk.construct(key_data)
        claims: dict[str, Any] = jose_jwt.decode(
            id_token,
            rsa_key,
            algorithms=["RS256"],
            audience=settings.GOOGLE_OAUTH_CLIENT_ID,
            options={"verify_at_hash": False},
        )
    except JWTError as exc:
        logger.warning("oauth.id_token.verify_failed", error=str(exc))
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid_id_token")

    # Verificar issuer
    if claims.get("iss") not in _GOOGLE_VALID_ISSUERS:
        logger.warning("oauth.id_token.invalid_iss", iss=claims.get("iss"))
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid_id_token")

    # Verificar nonce (replay protection)
    if claims.get("nonce") != expected_nonce:
        logger.warning("oauth.id_token.nonce_mismatch")
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid_id_token")

    return claims


# ── Service ───────────────────────────────────────────────────────────────────

class GoogleOAuthService:
    def __init__(
        self,
        session: AsyncSession,
        redis: Redis,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._session = session
        self._redis = redis
        self._http = http_client  # inyectado en tests para mockear Google
        self._user_repo = UserRepository(session)
        self._tenant_repo = TenantRepository(session)

    @asynccontextmanager
    async def _http_context(self) -> AsyncGenerator[httpx.AsyncClient, None]:
        """Provee un cliente HTTP.

        - Tests: devuelve el cliente inyectado tal cual (no lo cierra — el test lo gestiona).
        - Producción: crea un AsyncClient nuevo, lo cierra al salir del bloque.
        """
        if self._http is not None:
            yield self._http
        else:
            async with httpx.AsyncClient(timeout=10.0) as client:
                yield client

    # ── 1. Start ──────────────────────────────────────────────────────────────

    async def generate_start(self) -> OAuthStartResponse:
        """Genera state/nonce/PKCE, los guarda en Redis, devuelve auth URL."""
        state = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(32)
        code_verifier = _generate_code_verifier()
        code_challenge = _generate_code_challenge(code_verifier)

        session_data = {
            "nonce": nonce,
            "code_verifier": code_verifier,
        }
        await self._redis.set(
            f"oauth:state:{state}",
            json.dumps(session_data),
            ex=_STATE_TTL_SECONDS,
        )

        params = {
            "client_id": settings.GOOGLE_OAUTH_CLIENT_ID,
            "redirect_uri": settings.GOOGLE_OAUTH_REDIRECT_URI,
            "response_type": "code",
            "scope": " ".join([
                "openid", "email", "profile",
                "https://www.googleapis.com/auth/gmail.readonly",
                "https://www.googleapis.com/auth/gmail.compose",
                "https://www.googleapis.com/auth/spreadsheets.readonly",
                "https://www.googleapis.com/auth/drive.readonly",
                "https://www.googleapis.com/auth/documents.readonly",
                "https://www.googleapis.com/auth/photoslibrary.readonly",
            ]),
            "state": state,
            "nonce": nonce,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "access_type": "offline",  # pedir refresh_token para servicios Google
            "prompt": "consent",       # forzar pantalla de consentimiento para obtener refresh_token
        }
        authorization_url = "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(params)

        logger.info("oauth.start.generated", state_prefix=state[:8])
        return OAuthStartResponse(authorization_url=authorization_url)

    # ── 2. Callback ───────────────────────────────────────────────────────────

    async def handle_callback(self, code: str, state: str) -> str:
        """Procesa el callback de Google. Devuelve session_id para el exchange.

        El session_id se persiste en Redis con TTL de 60 segundos.
        El frontend debe llamar POST /exchange dentro de ese tiempo.

        Raises HTTPException en cualquier fallo de validación.
        """
        # 2a. GETDEL del state — single-use atómico
        raw = await self._redis.getdel(f"oauth:state:{state}")
        if raw is None:
            logger.warning("oauth.callback.invalid_state", state_prefix=state[:8])
            raise HTTPException(status.HTTP_409_CONFLICT, "invalid_or_expired_state")

        oauth_session = json.loads(raw)
        nonce = oauth_session["nonce"]
        code_verifier = oauth_session["code_verifier"]

        # 2b. Intercambiar code por tokens con Google
        async with self._http_context() as http:
            try:
                token_resp = await http.post(
                    _GOOGLE_TOKEN_URL,
                    data={
                        "code": code,
                        "client_id": settings.GOOGLE_OAUTH_CLIENT_ID,
                        "client_secret": settings.GOOGLE_OAUTH_CLIENT_SECRET,
                        "redirect_uri": settings.GOOGLE_OAUTH_REDIRECT_URI,
                        "grant_type": "authorization_code",
                        "code_verifier": code_verifier,
                    },
                )
                token_resp.raise_for_status()
            except httpx.HTTPStatusError as exc:
                logger.warning(
                    "oauth.callback.token_exchange_failed",
                    status=exc.response.status_code,
                )
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "token_exchange_failed")
            except httpx.HTTPError as exc:
                logger.error("oauth.callback.google_unavailable", error=str(exc))
                raise HTTPException(status.HTTP_502_BAD_GATEWAY, "google_unavailable")

            token_data = token_resp.json()
            id_token_str = token_data.get("id_token")
            if not id_token_str:
                logger.warning("oauth.callback.no_id_token")
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid_token_response")

            # 2c. Verificar id_token y extraer claims
            claims = await _verify_id_token(id_token_str, nonce, http)

        # Tokens de workspace (opcionales — se persisten si están disponibles)
        ws_refresh_token: str | None = token_data.get("refresh_token")
        ws_access_token: str = token_data.get("access_token", "")
        ws_scopes: list[str] = [s for s in token_data.get("scope", "").split() if s]
        ws_expires_in: int = int(token_data.get("expires_in", 3600))

        # 2d. Fail-closed si email no verificado
        if not claims.get("email_verified"):
            logger.warning(
                "oauth.callback.email_not_verified",
                sub=claims.get("sub", "")[:8],
            )
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "email_not_verified")

        provider_subject: str = claims["sub"]
        provider_email: str = claims["email"].lower()
        full_name: str = claims.get("name", provider_email.split("@")[0])

        # 2e. Resolver identidad → resultado
        result = await self._resolve_identity(
            provider_subject=provider_subject,
            provider_email=provider_email,
            full_name=full_name,
        )

        # 2f. Persistir tokens de workspace si obtuvimos refresh_token (non-fatal)
        if result["type"] == "auth" and ws_refresh_token:
            payload = result["payload"]
            try:
                ws_user_id = uuid.UUID(payload["user"]["user_id"])
                ws_tenant_id = uuid.UUID(payload["user"]["tenant_id"])
                await self._save_workspace_tokens(
                    user_id=ws_user_id,
                    tenant_id=ws_tenant_id,
                    provider_email=provider_email,
                    refresh_token=ws_refresh_token,
                    access_token=ws_access_token,
                    scopes=ws_scopes,
                    expires_in=ws_expires_in,
                )
            except Exception as exc:
                logger.warning("oauth.callback.workspace_save_failed", error=str(exc))

        # 2h. Guardar resultado en Redis (TTL corto) y devolver session_id
        exchange_session_id = secrets.token_urlsafe(32)
        await self._redis.set(
            f"oauth:exchange:{exchange_session_id}",
            json.dumps(result),
            ex=_EXCHANGE_TTL_SECONDS,
        )

        return exchange_session_id

    # ── 3. Exchange ───────────────────────────────────────────────────────────

    async def exchange_session(
        self, session_id: str
    ) -> AuthResponse | OAuthLinkRequiredResponse:
        """GETDEL del resultado del callback. Single-use.

        Devuelve AuthResponse (login exitoso) o OAuthLinkRequiredResponse.
        """
        raw = await self._redis.getdel(f"oauth:exchange:{session_id}")
        if raw is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid_or_expired_session")

        data = json.loads(raw)

        if data["type"] == "auth":
            return AuthResponse(**data["payload"])
        elif data["type"] == "link_required":
            return OAuthLinkRequiredResponse(**data["payload"])
        else:
            logger.error("oauth.exchange.unknown_type", type=data.get("type"))
            raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "internal_error")

    # ── 4. Link Pending ───────────────────────────────────────────────────────

    async def complete_link(
        self,
        pending_session_id: str,
        email: str,
        password: str,
    ) -> AuthResponse:
        """Completa el flujo link_required.

        1. GETDEL link session (single-use atómico).
        2. Autentica usuario con password.
        3. Crea UserAuthIdentity vinculando la identidad Google al usuario local.
        4. Devuelve AuthResponse.

        Fail-closed:
        - Session inválida/expirada → 400
        - Credenciales incorrectas → 401
        - Usuario inactivo → 403 (preserva el flujo de verificación de email)
        - Identidad ya vinculada a otro usuario → 409
        """
        # 4a. GETDEL link session (single-use)
        raw = await self._redis.getdel(f"oauth:link:{pending_session_id}")
        if raw is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid_or_expired_session")

        link_data = json.loads(raw)

        # 4b. Verificar que el email del cliente coincide con el email de la sesión OAuth.
        # Sin esta verificación, un atacante podría iniciar OAuth para A@gmail.com
        # y completar el link autenticándose con credenciales de una cuenta local B.
        if link_data["provider_email"] != email.lower():
            logger.warning(
                "oauth.link.email_mismatch",
                session_email=link_data["provider_email"],
                request_email=email.lower(),
            )
            raise HTTPException(status.HTTP_403_FORBIDDEN, "email_mismatch")

        # 4c. Autenticar con password
        user = await self._user_repo.get_by_email_any_tenant(email.lower())
        if user is None or not verify_password(password, user.password_hash):
            logger.warning("oauth.link.bad_credentials", email=email)
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid_credentials")

        if not user.is_active:
            logger.warning("oauth.link.inactive_user", user_id=str(user.user_id))
            raise HTTPException(status.HTTP_403_FORBIDDEN, "email_not_verified")

        # 4c. Vincular identidad
        identity = UserAuthIdentity(
            tenant_id=user.tenant_id,
            user_id=user.user_id,
            provider=link_data["provider"],
            provider_subject=link_data["provider_subject"],
            provider_email=link_data["provider_email"],
            last_login_at=datetime.now(UTC),
        )
        self._session.add(identity)
        try:
            await self._session.flush()
        except IntegrityError:
            logger.warning(
                "oauth.link.identity_conflict",
                provider=link_data["provider"],
                subject=link_data["provider_subject"][:8],
            )
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "identity_already_linked_to_another_account",
            )

        # 4d. Actualizar last_login_at
        user.last_login_at = datetime.now(UTC)
        await self._user_repo.save(user)

        tenant = await self._tenant_repo.get_by_id(user.tenant_id)
        if tenant is None:
            raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "internal_error")

        logger.info(
            "oauth.link.completed",
            user_id=str(user.user_id),
            provider=link_data["provider"],
        )
        return self._build_auth_response(user, tenant)

    # ── Private helpers ───────────────────────────────────────────────────────

    async def _save_workspace_tokens(
        self,
        user_id: uuid.UUID,
        tenant_id: uuid.UUID,
        provider_email: str,
        refresh_token: str,
        access_token: str,
        scopes: list[str],
        expires_in: int = 3600,
    ) -> None:
        """Persiste tokens de Google en user_google_workspace_connections (upsert).

        Solo se llama cuando GOOGLE_TOKEN_CIPHER_KEY está disponible.
        Si el cifrado falla (clave no configurada), lanza excepción que el caller captura.
        """
        from datetime import timedelta  # noqa: PLC0415
        from app.persistence.models.user_google_workspace import UserGoogleWorkspaceConnection  # noqa: PLC0415
        from app.application.security.token_cipher import encrypt_token  # noqa: PLC0415

        now = datetime.now(UTC)
        result = await self._session.execute(
            select(UserGoogleWorkspaceConnection).where(
                UserGoogleWorkspaceConnection.user_id == user_id
            )
        )
        existing = result.scalar_one_or_none()

        access_token_encrypted = encrypt_token(access_token)
        refresh_token_encrypted = encrypt_token(refresh_token)
        expires_at = now + timedelta(seconds=expires_in)

        if existing:
            existing.access_token_encrypted = access_token_encrypted
            existing.refresh_token_encrypted = refresh_token_encrypted
            existing.scopes_granted = scopes
            existing.revoked_at = None
            existing.connected_at = now
            existing.expires_at = expires_at
            existing.last_error_code = None
            existing.last_error_at = None
            existing.google_account_email = provider_email
            existing.updated_at = now
        else:
            conn = UserGoogleWorkspaceConnection(
                user_id=user_id,
                tenant_id=tenant_id,
                google_account_email=provider_email,
                access_token_encrypted=access_token_encrypted,
                refresh_token_encrypted=refresh_token_encrypted,
                scopes_granted=scopes,
                expires_at=expires_at,
            )
            self._session.add(conn)
        await self._session.flush()
        logger.info(
            "oauth.callback.workspace_tokens_saved",
            user_id=str(user_id),
            scopes_count=len(scopes),
        )

    async def _resolve_identity(
        self,
        provider_subject: str,
        provider_email: str,
        full_name: str,
    ) -> dict[str, Any]:
        """Determina qué hacer con la identidad Google.

        Returns un dict serializable con {"type": "auth"|"link_required", "payload": {...}}
        """
        # Caso 1: identidad ya vinculada → login directo
        result = await self._session.execute(
            select(UserAuthIdentity).where(
                UserAuthIdentity.provider == "google",
                UserAuthIdentity.provider_subject == provider_subject,
            )
        )
        identity = result.scalar_one_or_none()

        if identity is not None:
            user_result = await self._session.execute(
                select(User).where(User.user_id == identity.user_id)
            )
            user = user_result.scalar_one_or_none()
            if user is not None and user.is_active:
                tenant = await self._tenant_repo.get_by_id(user.tenant_id)
                if tenant is not None:
                    identity.last_login_at = datetime.now(UTC)
                    user.last_login_at = datetime.now(UTC)
                    await self._session.flush()
                    logger.info(
                        "oauth.callback.existing_identity",
                        user_id=str(user.user_id),
                    )
                    auth_resp = self._build_auth_response(user, tenant)
                    return {"type": "auth", "payload": auth_resp.model_dump(mode="json")}

        # Caso 2: email ya existe en cuenta local (activa o inactiva)
        existing_user = await self._user_repo.get_by_email_any_tenant(provider_email)
        if existing_user is not None:
            pending_session_id = secrets.token_urlsafe(32)
            await self._redis.set(
                f"oauth:link:{pending_session_id}",
                json.dumps({
                    "provider": "google",
                    "provider_subject": provider_subject,
                    "provider_email": provider_email,
                }),
                ex=_LINK_TTL_SECONDS,
            )
            logger.info(
                "oauth.callback.link_required",
                email=provider_email,
            )
            return {
                "type": "link_required",
                "payload": {
                    "status": "link_required",
                    "pending_oauth_session_id": pending_session_id,
                    "email": provider_email,
                    "provider": "google",
                },
            }

        # Caso 3: usuario nuevo → crear tenant + user + identity
        user, tenant = await self._create_social_user(
            provider_email=provider_email,
            provider_subject=provider_subject,
            full_name=full_name,
        )
        logger.info(
            "oauth.callback.new_user",
            user_id=str(user.user_id),
            tenant_id=str(tenant.tenant_id),
        )
        auth_resp = self._build_auth_response(user, tenant)
        return {"type": "auth", "payload": auth_resp.model_dump(mode="json")}

    async def _create_social_user(
        self,
        provider_email: str,
        provider_subject: str,
        full_name: str,
    ) -> tuple[User, Tenant]:
        """Crea Tenant + User + Subscription + MomentumProfile + UserAuthIdentity.

        No crea BusinessProfile — se hace en el flujo de onboarding.
        El usuario es is_active=True porque Google garantizó email_verified=True.
        password_hash = hash de UUID aleatorio (el usuario nunca lo usa).
        """
        # Tenant
        tenant = Tenant(
            legal_name=full_name,
            display_name=full_name,
            currency="ARS",
            pricing_reference_mode="MEP",
            status="ACTIVE",
        )
        await self._tenant_repo.save(tenant)

        # User — is_active=True, sin verificación de email
        random_password = str(uuid.uuid4())
        user = User(
            tenant_id=tenant.tenant_id,
            email=provider_email,
            full_name=full_name,
            password_hash=hash_password(random_password),
            role_code="OWNER",
            is_active=True,
            last_login_at=datetime.now(UTC),
        )
        await self._user_repo.save(user)

        # Subscription FREE
        subscription = Subscription(
            tenant_id=tenant.tenant_id,
            plan_code="FREE",
            billing_index_reference="MEP",
            seats_included=1,
            status="ACTIVE",
        )
        self._session.add(subscription)
        await self._session.flush()

        # MomentumProfile vacío
        momentum = MomentumProfile(
            tenant_id=tenant.tenant_id,
            improving_streak_weeks=0,
            milestones_json=[],
            updated_at=datetime.now(UTC),
        )
        self._session.add(momentum)
        await self._session.flush()

        # UserAuthIdentity
        identity = UserAuthIdentity(
            tenant_id=tenant.tenant_id,
            user_id=user.user_id,
            provider="google",
            provider_subject=provider_subject,
            provider_email=provider_email,
            last_login_at=datetime.now(UTC),
        )
        self._session.add(identity)
        await self._session.flush()

        return user, tenant

    def _build_auth_response(self, user: User, tenant: Tenant) -> AuthResponse:
        jwt_payload = {
            "sub": str(user.user_id),
            "tenant_id": str(tenant.tenant_id),
            "role_code": user.role_code,
        }
        return AuthResponse(
            access_token=create_access_token(jwt_payload),
            refresh_token=create_refresh_token(jwt_payload),
            token_type="bearer",
            expires_in=settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES * 60,
            user=UserInAuthResponse(
                user_id=user.user_id,
                email=user.email,
                full_name=user.full_name,
                role_code=user.role_code,
                tenant_id=tenant.tenant_id,
            ),
        )
