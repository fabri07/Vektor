"""Estado de integraciones externas disponibles para el tenant actual."""

from fastapi import APIRouter, Depends

from app.api.v1.deps import get_current_user
from app.config.settings import get_settings
from app.persistence.models.user import User
from app.schemas.integrations import GoogleAppStatus, GoogleIntegrationStatusResponse

router = APIRouter()

_GOOGLE_APPS = [
    GoogleAppStatus(
        id="gmail",
        label="Gmail",
        description="Borradores y lectura asistida de correos de proveedores.",
        available=False,
        connected=False,
        needs_reconnect=False,
        required_scopes=["gmail.readonly", "gmail.compose"],
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
        description="Exportar datos y reportes desde Véktor hacia Google Workspace.",
        available=False,
        connected=False,
        needs_reconnect=False,
        required_scopes=["spreadsheets", "documents"],
    ),
]


@router.get(
    "/google/status",
    response_model=GoogleIntegrationStatusResponse,
    summary="Estado de integraciones Google para el tenant actual",
)
async def google_status(_: User = Depends(get_current_user)) -> GoogleIntegrationStatusResponse:
    settings = get_settings()
    mcp_enabled = settings.ENABLE_GOOGLE_MCP_TOOLS
    mcp_server_configured = bool(settings.MCP_SERVER_URL)
    backend_available = mcp_enabled and mcp_server_configured

    apps = [
        GoogleAppStatus(
            **{
                **app.model_dump(),
                "available": backend_available,
                "connected": False,
                "needs_reconnect": False,
            }
        )
        for app in _GOOGLE_APPS
    ]

    if backend_available:
        message = (
            "Google MCP está habilitado en este entorno. El flujo de conexión por tenant "
            "todavía no está expuesto en la interfaz de Véktor."
        )
    else:
        message = (
            "Las integraciones Google no están habilitadas en este entorno o falta configurar "
            "el MCP server."
        )

    return GoogleIntegrationStatusResponse(
        mcp_enabled=mcp_enabled,
        mcp_server_configured=mcp_server_configured,
        connection_flow_available=False,
        connected=False,
        connected_at=None,
        last_error_code=None,
        apps=apps,
        message=message,
    )
