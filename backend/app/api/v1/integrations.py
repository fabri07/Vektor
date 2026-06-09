"""Estado y gestión de integraciones externas para el tenant actual.

GET  /integrations/google/status          — estado de conexión Google (por tenant+user)
POST /integrations/google/connect/start   — inicia flujo OAuth hacia el MCP server
POST /integrations/google/disconnect      — revoca la conexión OAuth
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import get_current_user, require_role
from app.application.services.mcp_diagnostics_service import run_google_mcp_diagnostics
from app.config.settings import get_settings
from app.integrations.mcp.exceptions import McpToolUnavailableError
from app.observability.logger import get_logger
from app.persistence.db.session import get_db_session
from app.persistence.models.google_mcp_connection import GoogleMcpConnection
from app.persistence.models.user import User
from app.schemas.integrations import (
    ConnectStartResponse,
    GoogleAppStatus,
    GoogleIntegrationStatusResponse,
    GoogleMcpDiagnosticsResponse,
)

router = APIRouter()
logger = get_logger(__name__)

_CONNECTING_TIMEOUT = timedelta(minutes=10)

_GOOGLE_SCOPES = [
    "gmail.readonly",
    "gmail.compose",
    "gmail.send",
    "calendar.events",
    "spreadsheets",
    "documents",
    "drive.readonly",
    "drive.file",
]

_GOOGLE_APPS = [
    GoogleAppStatus(
        id="gmail",
        label="Gmail",
        description="Borradores y lectura asistida de correos de proveedores.",
        available=False,
        connected=False,
        needs_reconnect=False,
        required_scopes=["gmail.readonly", "gmail.compose", "gmail.send"],
    ),
    GoogleAppStatus(
        id="calendar",
        label="Google Calendar",
        description="Crear recordatorios y consultar eventos del negocio.",
        available=False,
        connected=False,
        needs_reconnect=False,
        required_scopes=["calendar.events"],
    ),
    GoogleAppStatus(
        id="sheets_docs",
        label="Sheets y Docs",
        description="Exportar datos y reportes desde Véktor hacia Google.",
        available=False,
        connected=False,
        needs_reconnect=False,
        required_scopes=["spreadsheets", "documents"],
    ),
    GoogleAppStatus(
        id="drive",
        label="Google Drive",
        description="Buscar, leer y analizar archivos del negocio sin descargarlos localmente.",
        available=False,
        connected=False,
        needs_reconnect=False,
        required_scopes=["drive.readonly", "drive.file"],
    ),
]

_SHORT_SCOPE_TO_URL = {
    "gmail.readonly": "https://www.googleapis.com/auth/gmail.readonly",
    "gmail.compose": "https://www.googleapis.com/auth/gmail.compose",
    "gmail.send": "https://www.googleapis.com/auth/gmail.send",
    "calendar.events": "https://www.googleapis.com/auth/calendar.events",
    "spreadsheets": "https://www.googleapis.com/auth/spreadsheets",
    "documents": "https://www.googleapis.com/auth/documents",
    "drive.readonly": "https://www.googleapis.com/auth/drive.readonly",
    "drive.file": "https://www.googleapis.com/auth/drive.file",
}


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _has_required_scopes(granted: list[str], required: list[str]) -> bool:
    granted_set = set(granted)
    return all(
        scope in granted_set or _SHORT_SCOPE_TO_URL.get(scope) in granted_set for scope in required
    )


async def _get_connection(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
) -> GoogleMcpConnection | None:
    stmt = select(GoogleMcpConnection).where(
        GoogleMcpConnection.tenant_id == tenant_id,
        GoogleMcpConnection.user_id == user_id,
    )
    return (await db.execute(stmt)).scalar_one_or_none()


def _build_status_response(
    *,
    connection: GoogleMcpConnection | None,
    mcp_enabled: bool,
    mcp_server_configured: bool,
) -> GoogleIntegrationStatusResponse:
    backend_available = mcp_enabled and mcp_server_configured
    connection_state = connection.status if connection else "DISCONNECTED"
    connected = connection_state == "CONNECTED"
    scopes_granted = (
        list(connection.scopes_granted) if connection and connection.scopes_granted else []
    )
    connected_at = (
        connection.connected_at.isoformat() if connection and connection.connected_at else None
    )
    last_error_code = connection.last_error_code if connection else None

    apps = [
        GoogleAppStatus(
            **{
                **app.model_dump(),
                "available": backend_available,
                "connected": connected
                and _has_required_scopes(scopes_granted, app.required_scopes),
                "needs_reconnect": (
                    connection_state in ("RECONNECT_REQUIRED", "INSUFFICIENT_SCOPE", "ERROR")
                    or (connected and not _has_required_scopes(scopes_granted, app.required_scopes))
                ),
            }
        )
        for app in _GOOGLE_APPS
    ]

    if not backend_available:
        message = (
            "Las integraciones Google no están habilitadas en este entorno o falta configurar "
            "el MCP server."
        )
    elif connected:
        message = (
            "Google conectado. Drive y el resto de las herramientas disponibles "
            "ya pueden usarse desde Véktor."
        )
    elif connection_state == "CONNECTING":
        message = "Esperando que completes la autorización en Google..."
    elif connection_state == "ERROR" and last_error_code == "oauth_callback_timeout":
        message = "La autorización de Google expiró. Volvé a iniciar la conexión."
    elif connection_state == "ERROR":
        message = "No se pudo completar la conexión con Google. Intentá conectar de nuevo."
    elif connection_state in ("RECONNECT_REQUIRED", "INSUFFICIENT_SCOPE"):
        message = "La conexión Google requiere reconexión. Hacé click en Reconectar."
    else:
        message = "Google MCP está habilitado. Conectá tu cuenta para usar las integraciones."

    return GoogleIntegrationStatusResponse(
        mcp_enabled=mcp_enabled,
        mcp_server_configured=mcp_server_configured,
        connection_flow_available=backend_available,
        connection_state=connection_state,
        connected=connected,
        scopes_granted=scopes_granted,
        connected_at=connected_at,
        last_error_code=last_error_code,
        apps=apps,
        message=message,
    )


@router.get(
    "/google/status",
    response_model=GoogleIntegrationStatusResponse,
    summary="Estado de conexión Google para el usuario actual",
)
async def google_status(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> GoogleIntegrationStatusResponse:
    settings = get_settings()
    mcp_enabled = settings.ENABLE_GOOGLE_MCP_TOOLS
    mcp_server_configured = bool(settings.MCP_SERVER_URL)

    connection = await _get_connection(db, current_user.tenant_id, current_user.user_id)

    # Timeout de CONNECTING — independiente de si MCP está habilitado
    if connection and connection.status == "CONNECTING":
        pending_since = _as_utc(connection.updated_at or connection.created_at)
        if _utc_now() - pending_since > _CONNECTING_TIMEOUT:
            connection.status = "ERROR"
            connection.last_error_code = "oauth_callback_timeout"
            connection.state_token = None
            await db.commit()
            return _build_status_response(
                connection=connection,
                mcp_enabled=mcp_enabled,
                mcp_server_configured=mcp_server_configured,
            )

        # Solo consultar el MCP server si está habilitado y configurado
        if mcp_enabled and mcp_server_configured:
            try:
                from app.integrations.mcp.http_gateway import HttpMcpGateway  # noqa: PLC0415

                gateway = HttpMcpGateway(settings=settings)
                auth_status = await gateway.get_auth_status(
                    tenant_id=str(current_user.tenant_id),
                    user_id=str(current_user.user_id),
                )
                _last_err = auth_status.get("last_error")
                if auth_status.get("connected"):
                    connection.status = "CONNECTED"
                    connection.scopes_granted = auth_status.get("scopes_granted", [])
                    connection.connected_at = _utc_now()
                    connection.last_error_code = None
                    connection.state_token = None
                    await db.commit()
                elif _last_err not in (None, "pending_callback", "not_connected"):
                    connection.status = "ERROR"
                    connection.last_error_code = _last_err
                    connection.state_token = None
                    await db.commit()
            except Exception as exc:
                logger.warning(
                    "integrations.status_check_failed",
                    tenant_id=str(current_user.tenant_id),
                    user_id=str(current_user.user_id),
                    error=str(exc),
                )

    return _build_status_response(
        connection=connection,
        mcp_enabled=mcp_enabled,
        mcp_server_configured=mcp_server_configured,
    )


@router.post(
    "/google/connect/start",
    response_model=ConnectStartResponse,
    status_code=status.HTTP_200_OK,
    summary="Iniciar flujo OAuth de conexión Google",
)
async def google_connect_start(
    current_user: User = Depends(require_role("OWNER", "ADMIN")),
    db: AsyncSession = Depends(get_db_session),
) -> ConnectStartResponse:
    settings = get_settings()
    if not settings.ENABLE_GOOGLE_MCP_TOOLS or not settings.MCP_SERVER_URL:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Las integraciones Google no están habilitadas en este entorno.",
        )

    from app.integrations.mcp.http_gateway import HttpMcpGateway  # noqa: PLC0415

    gateway = HttpMcpGateway(settings=settings)
    try:
        result = await gateway.start_auth(
            tenant_id=str(current_user.tenant_id),
            user_id=str(current_user.user_id),
            scopes=_GOOGLE_SCOPES,
        )
    except McpToolUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="El MCP server no está disponible en este momento.",
        ) from exc

    auth_url = result.get("auth_url", "")
    mcp_state = result.get("state", "")
    if not auth_url:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="El MCP server no devolvió auth_url.",
        )

    # Upsert del registro de conexión
    connection = await _get_connection(db, current_user.tenant_id, current_user.user_id)
    if connection is None:
        connection = GoogleMcpConnection(
            id=uuid.uuid4(),
            tenant_id=current_user.tenant_id,
            user_id=current_user.user_id,
        )
        db.add(connection)

    connection.status = "CONNECTING"
    connection.state_token = mcp_state
    connection.scopes_granted = []
    connection.connected_at = None
    connection.last_error_code = None
    await db.commit()

    logger.info(
        "integrations.google_connect_start",
        tenant_id=str(current_user.tenant_id),
        user_id=str(current_user.user_id),
    )
    return ConnectStartResponse(
        auth_url=auth_url,
        required_scopes=_GOOGLE_SCOPES,
        state=mcp_state,
    )


@router.post(
    "/google/disconnect",
    summary="Desconectar Google del usuario actual",
)
async def google_disconnect(
    current_user: User = Depends(require_role("OWNER", "ADMIN")),
    db: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    settings = get_settings()
    connection = await _get_connection(db, current_user.tenant_id, current_user.user_id)

    # Intentar revocar en el MCP server (fail-silent)
    if settings.ENABLE_GOOGLE_MCP_TOOLS and settings.MCP_SERVER_URL and connection:
        try:
            from app.integrations.mcp.http_gateway import HttpMcpGateway  # noqa: PLC0415

            gateway = HttpMcpGateway(settings=settings)
            await gateway.revoke_auth(
                tenant_id=str(current_user.tenant_id),
                user_id=str(current_user.user_id),
            )
        except Exception as exc:
            logger.warning(
                "integrations.revoke_auth_failed",
                tenant_id=str(current_user.tenant_id),
                user_id=str(current_user.user_id),
                error=str(exc),
            )

    if connection:
        connection.status = "DISCONNECTED"
        connection.scopes_granted = []
        connection.connected_at = None
        connection.state_token = None
        connection.last_error_code = None
        await db.commit()

    logger.info(
        "integrations.google_disconnect",
        tenant_id=str(current_user.tenant_id),
        user_id=str(current_user.user_id),
    )
    return {"ok": True}


@router.get(
    "/google/diagnostics",
    response_model=GoogleMcpDiagnosticsResponse,
    summary="Diagnóstico de la integración Google MCP (SUPERADMIN)",
    dependencies=[Depends(require_role("SUPERADMIN"))],
)
async def google_mcp_diagnostics(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> GoogleMcpDiagnosticsResponse:
    """FASE 5: chequea config + conectividad de Google MCP y reporta qué falta.

    No expone secretos. Lo no verificable desde el backend (redirect URI en Google
    Cloud, app verification) se reporta como hint informativo.
    """
    data = await run_google_mcp_diagnostics(
        db, current_user.tenant_id, current_user.user_id
    )
    return GoogleMcpDiagnosticsResponse(**data)
