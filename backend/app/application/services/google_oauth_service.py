"""Google OAuth service — Sprint 2: Login social.

Responsabilidades:
  1. generate_start()        → genera state/nonce/PKCE, los guarda en Redis, devuelve auth URL
  2. handle_callback()       → valida state+PKCE, intercambia code, verifica id_token
                               guarda resultado en Redis, devuelve session_id para exchange
  3. exchange_session()      → GETDEL del resultado (single-use),
                               devuelve AuthResponse, LinkRequired o AccessRequestRequired
  4. complete_link()         → GETDEL del pending_oauth_session,
                               verifica password, vincula identidad

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
    → devuelve AuthResponse (identidad ya vinculada),
       OAuthLinkRequiredResponse (email ya existe en cuenta local)
       u OAuthAccessRequestRequiredResponse (email desconocido)

  POST /link-pending {pending_oauth_session_id, email, password}
    → GETDEL oauth:link:{id}  ← single-use atómico
    → autentica con password (fail si is_active=False)
    → vincula UserAuthIdentity
    → devuelve AuthResponse

**El login con Google NO acuña cuentas.** Un email que Google verificó pero que
no existe en Véktor abre una SOLICITUD de acceso, igual que el formulario
público: el registro es cerrado y la cuenta la acuña la aprobación manual del
dueño. Este archivo emite el prefill (`oauth:prefill:{token}`) que liga esa
solicitud a la identidad de Google; quien lo canjea es
`api/v1/access_requests.py`, y quien crea el `UserAuthIdentity` es
`AccessRequestService.approve()`.

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
from base64 import urlsafe_b64encode
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode

import httpx
from fastapi import HTTPException, status
from jose import JWTError, jwk
from jose import jwt as jose_jwt
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import get_settings
from app.observability.logger import get_logger
from app.persistence.models.tenant import Tenant
from app.persistence.models.user import User
from app.persistence.models.user_auth_identity import UserAuthIdentity
from app.persistence.repositories.tenant_repository import TenantRepository
from app.persistence.repositories.user_repository import UserRepository
from app.schemas.auth import AuthResponse, UserInAuthResponse
from app.schemas.oauth import (
    OAuthAccessRequestRequiredResponse,
    OAuthLinkRequiredResponse,
    OAuthStartResponse,
)
from app.utils.security import (
    create_access_token,
    create_refresh_token,
    verify_password,
)

logger = get_logger(__name__)
settings = get_settings()

# ── Constantes ────────────────────────────────────────────────────────────────
_GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
_GOOGLE_JWKS_URL = "https://www.googleapis.com/oauth2/v3/certs"
_GOOGLE_VALID_ISSUERS = {"accounts.google.com", "https://accounts.google.com"}

_STATE_TTL_SECONDS = 600  # 10 min — tiempo que el usuario tiene para completar el flujo OAuth
_EXCHANGE_TTL_SECONDS = 60  # 60 seg — ventana para que el frontend haga el exchange post-redirect
_LINK_TTL_SECONDS = 600  # 10 min — tiempo para completar el link_required
#: Prefill del alta por Google. Misma FORMA que `oauth:link:{id}`, pero TTL
#: mucho más largo a propósito: del otro lado hay un formulario de 16 campos,
#: con un aviso de confidencialidad para leer y una pregunta de facturación que
#: la gente piensa. Con 10 min el token se vencía seguido, y un prefill vencido
#: hace que la solicitud se persista sin `google_subject` — el aprobado termina
#: obligado a definir contraseña, que es justo la fricción que este camino
#: existía para evitar.
#:
#: Alargarlo no amplía la superficie: el token sigue siendo opaco, de un solo
#: uso (el GETDEL lo hace el POST de la solicitud) y su canje exige que el email
#: del formulario coincida con el de Google (403 `google_prefill_email_mismatch`).
_PREFILL_TTL_SECONDS = 2700  # 45 min
_PREFILL_KEY_PREFIX = "oauth:prefill:"

# Cache en memoria del JWKS de Google (se invalida cada hora)
_jwks_cache: dict[str, Any] | None = None
_jwks_cached_at: datetime | None = None
_JWKS_CACHE_TTL_SECONDS = 3600


# ── Prefill del alta por Google ───────────────────────────────────────────────


@dataclass(frozen=True)
class GooglePrefill:
    """Identidad de Google esperando que el visitante mande su solicitud.

    ``provider_subject`` NUNCA sale al browser: el endpoint público de prefill
    devuelve solo lo que el formulario tiene que mostrar (email y nombre). El
    subject se resuelve del lado del servidor al canjear el token.

    ``full_name`` es opcional a propósito: Google no siempre manda el claim
    ``name`` y derivarlo del email sería inventar el nombre del solicitante.
    """

    email: str
    full_name: str | None
    provider_subject: str
    provider: str = "google"


def _prefill_key(token: str) -> str:
    return f"{_PREFILL_KEY_PREFIX}{token}"


def _decode_prefill(raw: str | None) -> GooglePrefill | None:
    if raw is None:
        return None
    try:
        data = json.loads(raw)
        return GooglePrefill(
            email=data["email"],
            full_name=data["full_name"],
            provider_subject=data["provider_subject"],
            provider=data["provider"],
        )
    except (ValueError, KeyError, TypeError):  # pragma: no cover - payload corrupto
        logger.warning("oauth.prefill.corrupt_payload")
        return None


async def read_google_prefill(redis: Redis, token: str) -> GooglePrefill | None:
    """Lee el prefill **sin consumirlo**. Devuelve ``None`` si no existe o venció.

    Es un GET y no un GETDEL a propósito: el formulario público llama acá para
    mostrar el email y el nombre, y recién DESPUÉS manda el mismo token en el
    POST de la solicitud, que es donde se resuelve el ``provider_subject``. Si
    esta lectura borrara la key, el POST llegaría con un token inexistente y el
    linkeo con Google se perdería en silencio.
    """
    return _decode_prefill(await redis.get(_prefill_key(token)))


async def consume_google_prefill(redis: Redis, token: str) -> GooglePrefill | None:
    """GETDEL del prefill: **esta es la única toma del token** (single-use).

    Devuelve ``None`` si el token no existe, venció o ya se canjeó.
    """
    return _decode_prefill(await redis.getdel(_prefill_key(token)))


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
    jwks: dict[str, Any] = resp.json()
    _jwks_cache = jwks
    _jwks_cached_at = now
    return jwks


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
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid_id_token") from exc

    kid = header.get("kid")
    if not kid:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid_id_token")

    try:
        jwks = await _get_google_jwks(http)
    except httpx.HTTPError as exc:
        logger.error("oauth.jwks.fetch_failed", error=str(exc))
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "google_unavailable") from exc

    key_data = next((k for k in jwks.get("keys", []) if k.get("kid") == kid), None)
    if key_data is None:
        # Invalidar cache y reintentar una vez (rotación de clave)
        global _jwks_cache  # noqa: PLW0603
        _jwks_cache = None
        try:
            jwks = await _get_google_jwks(http)
        except httpx.HTTPError as exc:
            logger.error("oauth.jwks.retry_failed", error=str(exc))
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, "google_unavailable") from exc
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
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid_id_token") from exc

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
            "scope": " ".join(["openid", "email", "profile"]),
            "state": state,
            "nonce": nonce,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "access_type": "offline",  # pedir refresh_token para servicios Google
            "prompt": "consent",  # forzar pantalla de consentimiento para obtener refresh_token
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
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "token_exchange_failed") from exc
            except httpx.HTTPError as exc:
                logger.error("oauth.callback.google_unavailable", error=str(exc))
                raise HTTPException(status.HTTP_502_BAD_GATEWAY, "google_unavailable") from exc

            token_data = token_resp.json()
            id_token_str = token_data.get("id_token")
            if not id_token_str:
                logger.warning("oauth.callback.no_id_token")
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid_token_response")

            # 2c. Verificar id_token y extraer claims
            claims = await _verify_id_token(id_token_str, nonce, http)

        # 2d. Fail-closed si email no verificado
        if not claims.get("email_verified"):
            logger.warning(
                "oauth.callback.email_not_verified",
                sub=claims.get("sub", "")[:8],
            )
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "email_not_verified")

        provider_subject: str = claims["sub"]
        provider_email: str = claims["email"].lower()
        # Sin claim `name` el nombre queda en None y el formulario lo pide: la
        # parte local del email NO es el nombre del solicitante, y prellenar con
        # eso sería inventarle una respuesta a la ficha que el dueño revisa.
        full_name: str | None = claims.get("name")

        # 2e. Resolver identidad → resultado
        result = await self._resolve_identity(
            provider_subject=provider_subject,
            provider_email=provider_email,
            full_name=full_name,
        )

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
    ) -> AuthResponse | OAuthLinkRequiredResponse | OAuthAccessRequestRequiredResponse:
        """GETDEL del resultado del callback. Single-use.

        Devuelve AuthResponse (login exitoso), OAuthLinkRequiredResponse o
        OAuthAccessRequestRequiredResponse (email que no tiene cuenta: el alta
        pasa por una solicitud de acceso, no por un tenant nuevo).
        """
        raw = await self._redis.getdel(f"oauth:exchange:{session_id}")
        if raw is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid_or_expired_session")

        data = json.loads(raw)

        if data["type"] == "auth":
            return AuthResponse(**data["payload"])
        elif data["type"] == "link_required":
            return OAuthLinkRequiredResponse(**data["payload"])
        elif data["type"] == "access_request_required":
            return OAuthAccessRequestRequiredResponse(**data["payload"])
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
            ) from None

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

    async def _resolve_identity(
        self,
        provider_subject: str,
        provider_email: str,
        full_name: str | None,
    ) -> dict[str, Any]:
        """Determina qué hacer con la identidad Google.

        Returns un dict serializable con
        ``{"type": "auth"|"link_required"|"access_request_required", "payload": {...}}``
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
                json.dumps(
                    {
                        "provider": "google",
                        "provider_subject": provider_subject,
                        "provider_email": provider_email,
                    }
                ),
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

        # Caso 3: email desconocido → NO se acuña ninguna cuenta.
        #
        # El registro de Véktor es cerrado: el alta la hace la aprobación manual
        # del dueño (`AccessRequestService.approve`), que es la única que asigna
        # el vertical operativo. Entrar con Google no puede ser un atajo que
        # esquive esa revisión, así que este camino termina exactamente donde
        # termina el formulario público: en una solicitud de acceso.
        #
        # Lo único que se persiste acá es el prefill en Redis, para que la
        # solicitud quede ligada a esta identidad verificada por Google y el
        # usuario aprobado pueda entrar con Google sin pasar por la contraseña.
        prefill_token = secrets.token_urlsafe(32)
        await self._redis.set(
            _prefill_key(prefill_token),
            json.dumps(
                {
                    "provider": "google",
                    "provider_subject": provider_subject,
                    "email": provider_email,
                    "full_name": full_name,
                }
            ),
            ex=_PREFILL_TTL_SECONDS,
        )
        logger.info("oauth.callback.access_request_required", email=provider_email)
        return {
            "type": "access_request_required",
            "payload": {
                "prefill_token": prefill_token,
                "email": provider_email,
                "full_name": full_name,
            },
        }

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
                phone=user.phone,
            ),
        )
