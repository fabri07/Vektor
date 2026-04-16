"""Workspace endpoints — Sprint 3: Google Workspace Connect.

Endpoints (todos bajo ENABLE_GOOGLE_WORKSPACE_MCP=False → 404):
  POST   /workspace/google/connect/start      → {authorization_url}
  GET    /workspace/google/connect/callback   → RedirectResponse al frontend
  POST   /workspace/google/connect/exchange   → 200 (conexión guardada)
  DELETE /workspace/google/disconnect         → {disconnected: true}
  GET    /workspace/google/status             → WorkspaceStatusResponse

Seguridad:
  - start/exchange/disconnect/status requieren JWT (get_current_user)
  - callback NO tiene JWT (browser redirect de Google)
  - ws:state incluye user_id — exchange valida que coincide con JWT
  - GETDEL atómico en state y exchange (single-use)
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from app.observability.logger import get_logger

_log = get_logger(__name__)
from fastapi.responses import RedirectResponse
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import get_current_user
from app.application.services.workspace_connect_service import WorkspaceConnectService
from app.config.settings import get_settings
from app.integrations.google_workspace.apps import GOOGLE_WORKSPACE_APPS
from app.integrations.google_workspace.gateway import GoogleWorkspaceGateway
from app.main import limiter
from app.persistence.db.redis import get_redis
from app.persistence.db.session import get_db_session
from app.persistence.models.user import User
from app.persistence.models.user_google_workspace import UserGoogleWorkspaceConnection
from app.schemas.workspace import (
    WorkspaceAppStatus,
    WorkspaceConnectExchangeRequest,
    WorkspaceConnectStartRequest,
    WorkspaceConnectStartResponse,
    WorkspaceDisconnectResponse,
    WorkspaceStatusResponse,
)

router = APIRouter()
settings = get_settings()


def _require_workspace_mcp() -> None:
    """Dependency: 404 cuando ENABLE_GOOGLE_WORKSPACE_MCP está desactivado."""
    if not settings.ENABLE_GOOGLE_WORKSPACE_MCP:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Google Workspace integration is not enabled.",
        )


def _build_app_statuses(
    scopes_granted: list[str],
    connected: bool,
    last_error_code: str | None = None,
) -> list[WorkspaceAppStatus]:
    granted = set(scopes_granted)
    statuses: list[WorkspaceAppStatus] = []
    for app in GOOGLE_WORKSPACE_APPS.values():
        required = list(app.required_scopes)
        has_scopes = all(scope in granted for scope in required)
        app_connected = connected and app.available and has_scopes
        statuses.append(
            WorkspaceAppStatus(
                id=app.id,
                label=app.label,
                description=app.description,
                available=app.available,
                connected=app_connected,
                needs_reconnect=connected and app.available and (not has_scopes or bool(last_error_code)),
                required_scopes=required,
            )
        )
    return statuses


# ── 1. Start ──────────────────────────────────────────────────────────────────

@router.post(
    "/google/connect/start",
    response_model=WorkspaceConnectStartResponse,
    summary="Iniciar conexión de Google Workspace",
    dependencies=[Depends(_require_workspace_mcp)],
)
@limiter.limit("10/5minutes")
async def workspace_connect_start(
    request: Request,
    body: WorkspaceConnectStartRequest | None = None,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
    redis: Redis = Depends(get_redis),
) -> WorkspaceConnectStartResponse:
    """Genera state/PKCE y devuelve authorization_url con scopes de Workspace.

    El state tiene binding user_id — el exchange posterior valida que el mismo
    usuario que inició el flujo sea quien lo complete.
    """
    svc = WorkspaceConnectService(session, redis)
    url = await svc.generate_start(
        current_user.user_id,
        current_user.tenant_id,
        app_ids=body.app_ids if body else None,
    )
    return WorkspaceConnectStartResponse(authorization_url=url)


# ── 2. Callback (browser redirect) ───────────────────────────────────────────

@router.get(
    "/google/connect/callback",
    summary="Callback de Google Workspace (browser redirect)",
    dependencies=[Depends(_require_workspace_mcp)],
)
@limiter.limit("20/5minutes")
async def workspace_connect_callback(
    request: Request,
    code: str | None = Query(default=None, description="Authorization code de Google"),
    state: str | None = Query(default=None, description="State CSRF"),
    error: str | None = Query(default=None, description="Error devuelto por Google"),
    session: AsyncSession = Depends(get_db_session),
    redis: Redis = Depends(get_redis),
) -> RedirectResponse:
    """Recibe el redirect de Google tras el consentimiento de Workspace.

    Intercambia el code, obtiene tokens, guarda en Redis (60 seg) y
    redirige al frontend con exchange_session_id.
    """
    frontend_callback = f"{settings.FRONTEND_URL}/workspace/connect/callback"

    if error:
        return RedirectResponse(
            url=f"{frontend_callback}?error={error}",
            status_code=status.HTTP_302_FOUND,
        )

    if not code or not state:
        return RedirectResponse(
            url=f"{frontend_callback}?error=missing_code_or_state",
            status_code=status.HTTP_302_FOUND,
        )

    try:
        svc = WorkspaceConnectService(session, redis)
        exchange_id = await svc.handle_callback(code=code, state=state)
    except HTTPException as exc:
        return RedirectResponse(
            url=f"{frontend_callback}?error={exc.detail}",
            status_code=status.HTTP_302_FOUND,
        )

    return RedirectResponse(
        url=f"{frontend_callback}?exchange_session_id={exchange_id}",
        status_code=status.HTTP_302_FOUND,
    )


# ── 3. Exchange ───────────────────────────────────────────────────────────────

@router.post(
    "/google/connect/exchange",
    status_code=status.HTTP_200_OK,
    summary="Completar conexión de Google Workspace",
    dependencies=[Depends(_require_workspace_mcp)],
)
@limiter.limit("10/5minutes")
async def workspace_connect_exchange(
    request: Request,
    body: WorkspaceConnectExchangeRequest,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
    redis: Redis = Depends(get_redis),
) -> dict:  # type: ignore[type-arg]
    """GETDEL del exchange. Valida user binding y persiste la conexión Workspace.

    Retorna 403 si el exchange_session_id fue iniciado por otro usuario.
    """
    svc = WorkspaceConnectService(session, redis)
    await svc.complete_exchange(
        exchange_session_id=body.exchange_session_id,
        current_user_id=current_user.user_id,
        current_tenant_id=current_user.tenant_id,
    )
    await session.commit()
    return {"connected": True}


# ── 4. Disconnect ─────────────────────────────────────────────────────────────

@router.delete(
    "/google/disconnect",
    response_model=WorkspaceDisconnectResponse,
    summary="Desconectar Google Workspace",
    dependencies=[Depends(_require_workspace_mcp)],
)
@limiter.limit("10/hour")
async def workspace_disconnect(
    request: Request,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
    redis: Redis = Depends(get_redis),
) -> WorkspaceDisconnectResponse:
    """Soft revoke de la conexión Workspace.

    Idempotente — si ya estaba revocada retorna 200 igual.
    El revoke remoto en Google es best-effort (fallo no bloquea).
    """
    gateway = GoogleWorkspaceGateway(
        session=session,
        redis=redis,
        user_id=current_user.user_id,
        tenant_id=current_user.tenant_id,
    )
    await gateway.disconnect()
    await session.commit()
    return WorkspaceDisconnectResponse(disconnected=True)


# ── 5. Status ─────────────────────────────────────────────────────────────────

@router.get(
    "/google/status",
    response_model=WorkspaceStatusResponse,
    summary="Estado de la conexión Google Workspace",
    dependencies=[Depends(_require_workspace_mcp)],
)
async def workspace_status(
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> WorkspaceStatusResponse:
    """Devuelve el estado actual de la conexión Workspace del usuario."""
    result = await session.execute(
        select(UserGoogleWorkspaceConnection).where(
            UserGoogleWorkspaceConnection.user_id == current_user.user_id,
            UserGoogleWorkspaceConnection.tenant_id == current_user.tenant_id,
        )
    )
    conn = result.scalar_one_or_none()

    if conn is None or not conn.is_active:
        return WorkspaceStatusResponse(
            connected=False,
            apps=_build_app_statuses([], connected=False),
        )

    return WorkspaceStatusResponse(
        connected=True,
        google_account_email=conn.google_account_email,
        scopes_granted=conn.scopes_granted,
        apps=_build_app_statuses(conn.scopes_granted, connected=True, last_error_code=conn.last_error_code),
        connected_at=conn.connected_at,
        last_error_code=conn.last_error_code,
    )
