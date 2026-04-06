"""OAuth endpoints — Sprint 2: Google Login.

Endpoints:
  POST /auth/oauth/google/start      → devuelve {authorization_url}
  GET  /auth/oauth/google/callback   → procesa redirect de Google → redirige al frontend
  POST /auth/oauth/google/exchange   → GETDEL resultado del callback → AuthResponse | LinkRequired
  POST /auth/oauth/google/link-pending → completa link_required con password

Guard: ENABLE_GOOGLE_LOGIN=False → 404 en los 4 endpoints.
"""

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import RedirectResponse
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.google_oauth_service import GoogleOAuthService
from app.config.settings import get_settings
from app.main import limiter
from app.persistence.db.redis import get_redis
from app.persistence.db.session import get_db_session
from app.schemas.auth import AuthResponse
from app.schemas.oauth import (
    OAuthExchangeRequest,
    OAuthLinkPendingRequest,
    OAuthLinkRequiredResponse,
    OAuthStartResponse,
)

router = APIRouter()
settings = get_settings()


def _require_google_login() -> None:
    """Dependency: falla con 404 si ENABLE_GOOGLE_LOGIN está desactivado."""
    if not settings.ENABLE_GOOGLE_LOGIN:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Google login is not enabled.",
        )


@router.post(
    "/google/start",
    response_model=OAuthStartResponse,
    summary="Iniciar flujo OAuth con Google",
    dependencies=[Depends(_require_google_login)],
)
@limiter.limit("10/5minutes")
async def oauth_google_start(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    redis: Redis = Depends(get_redis),
) -> OAuthStartResponse:
    """Genera state/nonce/PKCE y devuelve la authorization URL de Google.

    El frontend debe redirigir al usuario a esa URL.
    """
    svc = GoogleOAuthService(session, redis)
    return await svc.generate_start()


@router.get(
    "/google/callback",
    summary="Callback OAuth de Google (browser redirect)",
    dependencies=[Depends(_require_google_login)],
    # No response_model: devuelve RedirectResponse
)
@limiter.limit("20/5minutes")
async def oauth_google_callback(
    request: Request,
    code: str | None = Query(default=None, description="Authorization code de Google"),
    state: str | None = Query(default=None, description="State para validación CSRF"),
    error: str | None = Query(default=None, description="Error devuelto por Google"),
    session: AsyncSession = Depends(get_db_session),
    redis: Redis = Depends(get_redis),
) -> RedirectResponse:
    """Recibe el redirect de Google tras el consentimiento del usuario.

    Procesa el code, verifica id_token, guarda resultado en Redis (60 seg)
    y redirige al frontend con el session_id para que haga el exchange.

    Errores redirigen al frontend con ?error=... para que los muestre.
    """
    frontend_callback = f"{settings.FRONTEND_URL}/oauth/callback"

    # Error devuelto por Google (usuario canceló, etc.)
    if error:
        return RedirectResponse(
            url=f"{frontend_callback}?error={error}",
            status_code=status.HTTP_302_FOUND,
        )

    # Parámetros requeridos cuando no hay error
    if not code or not state:
        return RedirectResponse(
            url=f"{frontend_callback}?error=missing_code_or_state",
            status_code=status.HTTP_302_FOUND,
        )

    try:
        svc = GoogleOAuthService(session, redis)
        exchange_session_id = await svc.handle_callback(code=code, state=state)
    except HTTPException as exc:
        return RedirectResponse(
            url=f"{frontend_callback}?error={exc.detail}",
            status_code=status.HTTP_302_FOUND,
        )

    return RedirectResponse(
        url=f"{frontend_callback}?session_id={exchange_session_id}",
        status_code=status.HTTP_302_FOUND,
    )


@router.post(
    "/google/exchange",
    summary="Obtener JWT tras callback de Google",
    dependencies=[Depends(_require_google_login)],
)
@limiter.limit("20/5minutes")
async def oauth_google_exchange(
    request: Request,
    body: OAuthExchangeRequest,
    session: AsyncSession = Depends(get_db_session),
    redis: Redis = Depends(get_redis),
) -> AuthResponse | OAuthLinkRequiredResponse:
    """Intercambia el session_id del callback por un JWT o respuesta link_required.

    El session_id tiene TTL de 60 segundos y es single-use (GETDEL).
    """
    svc = GoogleOAuthService(session, redis)
    return await svc.exchange_session(body.session_id)


@router.post(
    "/google/link-pending",
    response_model=AuthResponse,
    summary="Completar vinculación de cuenta Google con cuenta local",
    dependencies=[Depends(_require_google_login)],
)
@limiter.limit("10/5minutes")
async def oauth_google_link_pending(
    request: Request,
    body: OAuthLinkPendingRequest,
    session: AsyncSession = Depends(get_db_session),
    redis: Redis = Depends(get_redis),
) -> AuthResponse:
    """Completa el flujo link_required.

    El pending_oauth_session_id es single-use (TTL 10 min).
    Tras autenticar con password, vincula la identidad Google y emite JWT.
    """
    svc = GoogleOAuthService(session, redis)
    return await svc.complete_link(
        pending_session_id=body.pending_oauth_session_id,
        email=str(body.email),
        password=body.password,
    )
