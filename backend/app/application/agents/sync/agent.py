"""AgentSync — sincronización y lectura de datos de Google vía MCP."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from app.application.agents.base import BaseAgent
from app.application.agents.shared.schemas import ActionType, AgentRequest, AgentResponse, Confidence, RiskLevel
from app.config.settings import get_settings
from app.integrations.mcp.exceptions import McpToolAuthError
from app.integrations.mcp.google_mcp_service import GoogleMcpService
from app.observability.logger import get_logger

if TYPE_CHECKING:
    from app.application.ports.mcp_gateway import McpToolGateway

logger = get_logger(__name__)

# Regex para extraer IDs de URLs de Google
_SHEETS_ID_RE = re.compile(r"/spreadsheets/d/([a-zA-Z0-9_-]+)")
_DOCS_ID_RE = re.compile(r"/document/d/([a-zA-Z0-9_-]+)")
# ID solo (sin URL), largo típico ≥ 20 chars alfanuméricos + guiones/underscores
_BARE_ID_RE = re.compile(r"(?<![/\w])([a-zA-Z0-9_-]{20,})(?![/\w])")

_SYNC_TYPES = {
    "export_sales_to_sheets": {
        "keywords": ("exportar ventas", "ventas a sheets", "exportar a google sheets", "subir ventas"),
        "tool": "google.sheets.append_rows",
        "summary": "Exportar ventas a Google Sheets.",
        "needs_sheet": True,
        "needs_doc": False,
    },
    "export_report_to_docs": {
        "keywords": ("exportar reporte", "reporte a docs", "informe a google docs", "generar doc"),
        "tool": "google.docs.create_document",
        "summary": "Exportar reporte a Google Docs.",
        "needs_sheet": False,
        "needs_doc": False,  # crea un doc nuevo, no necesita ID previo
    },
    "import_from_sheets": {
        "keywords": ("importar desde sheets", "importar de google", "traer datos de sheets", "leer sheets"),
        "tool": "google.sheets.read_range",
        "summary": "Importar datos desde Google Sheets.",
        "needs_sheet": True,
        "needs_doc": False,
    },
    "import_from_drive": {
        "keywords": (
            "google drive",
            "buscar en drive",
            "leer drive",
            "archivos en drive",
            "carpeta de drive",
            "analizar drive",
        ),
        "tool": "google.drive.read_file",
        "summary": "Buscar y leer archivos desde Google Drive.",
        "needs_sheet": False,
        "needs_doc": False,
    },
}


class AgentSync(BaseAgent):
    agent_name = "agent_sync"

    def __init__(
        self,
        gateway: "McpToolGateway | None" = None,
        tenant_id: str | None = None,
    ) -> None:
        self._gateway = gateway
        self._tenant_id = tenant_id

    async def process(self, request: AgentRequest) -> AgentResponse:
        message_lower = request.message.lower()

        if self._gateway is None:
            return AgentResponse(
                request_id=request.request_id,
                agent_name=self.agent_name,
                status="requires_google_auth",
                risk_level=RiskLevel.LOW,
                confidence=Confidence.HIGH,
                result={
                    "action_type": ActionType.SYNC_TO_GOOGLE,
                    "mode": "informational",
                    "message": "Necesito que la integración de Google esté disponible para sincronizar datos.",
                },
            )

        sync_type = self._classify_sync_type(message_lower)
        if sync_type is None:
            return AgentResponse(
                request_id=request.request_id,
                agent_name=self.agent_name,
                status="requires_clarification",
                risk_level=RiskLevel.LOW,
                confidence=Confidence.LOW,
                question=(
                    "¿Qué tipo de sincronización con Google querés hacer?\n"
                    "- Exportar ventas a Google Sheets\n"
                    "- Exportar reporte a Google Docs\n"
                    "- Importar datos desde Google Sheets\n"
                    "- Buscar y leer archivos de Google Drive"
                ),
                result={"summary": "Tipo de sincronización no identificado."},
            )

        if sync_type == "import_from_drive":
            return await self._read_drive_content(request)

        cfg = _SYNC_TYPES[sync_type]

        # Tipos que necesitan spreadsheet_id: pedirlo si no está en el mensaje
        if cfg["needs_sheet"]:
            spreadsheet_id = self._extract_spreadsheet_id(request.message)
            if not spreadsheet_id:
                return AgentResponse(
                    request_id=request.request_id,
                    agent_name=self.agent_name,
                    status="requires_clarification",
                    risk_level=RiskLevel.LOW,
                    confidence=Confidence.HIGH,
                    requires_approval=False,
                    question=(
                        "Necesito el link de tu archivo de Google Sheets.\n"
                        "Pegá la URL completa, por ejemplo:\n"
                        "https://docs.google.com/spreadsheets/d/tu-id-aqui/edit"
                    ),
                    result={
                        "action_type": ActionType.SYNC_TO_GOOGLE,
                        "sync_type": sync_type,
                        "summary": "Falta el link del Google Sheets.",
                    },
                )

            payload = {
                "sync_type": sync_type,
                "spreadsheet_id": spreadsheet_id,
                "range_name": "Sheet1",
                "mode": "mcp",
                "raw_message": request.message,
            }
            summary = f"{cfg['summary']} Archivo: {spreadsheet_id[:20]}..."
            return AgentResponse(
                request_id=request.request_id,
                agent_name=self.agent_name,
                status="requires_approval",
                risk_level=RiskLevel.MEDIUM,
                confidence=Confidence.HIGH,
                requires_approval=True,
                result={
                    "action_type": ActionType.SYNC_TO_GOOGLE,
                    "sync_type": sync_type,
                    "mcp_tool": cfg["tool"],
                    "summary": summary,
                    "mode": "mcp",
                    "payload": payload,
                },
            )

        # export_report_to_docs: crea un doc nuevo, no necesita ID previo
        payload = {
            "sync_type": sync_type,
            "mode": "mcp",
            "raw_message": request.message,
        }
        return AgentResponse(
            request_id=request.request_id,
            agent_name=self.agent_name,
            status="requires_approval",
            risk_level=RiskLevel.MEDIUM,
            confidence=Confidence.HIGH,
            requires_approval=True,
            result={
                "action_type": ActionType.SYNC_TO_GOOGLE,
                "sync_type": sync_type,
                "mcp_tool": cfg["tool"],
                "summary": cfg["summary"],
                "mode": "mcp",
                "payload": payload,
            },
        )

    # ──────────────────────────────────────────────────────────────────────────

    def _classify_sync_type(self, message: str) -> str | None:
        for sync_type, cfg in _SYNC_TYPES.items():
            if any(kw in message for kw in cfg["keywords"]):
                return sync_type
        return None

    def _extract_spreadsheet_id(self, message: str) -> str | None:
        """Extrae el spreadsheet_id de una URL de Google Sheets o de un ID suelto."""
        m = _SHEETS_ID_RE.search(message)
        if m:
            return m.group(1)
        # ID suelto largo que el usuario pegó directamente
        m = _BARE_ID_RE.search(message)
        if m:
            return m.group(1)
        return None

    async def _read_drive_content(self, request: AgentRequest) -> AgentResponse:
        if self._gateway is None or self._tenant_id is None:
            return AgentResponse(
                request_id=request.request_id,
                agent_name=self.agent_name,
                status="requires_google_auth",
                risk_level=RiskLevel.LOW,
                confidence=Confidence.HIGH,
                result={
                    "action_type": ActionType.SYNC_TO_GOOGLE,
                    "mode": "informational",
                    "message": "Necesito acceso a Google para buscar y leer archivos de Drive.",
                },
            )

        drive_query = self._extract_drive_query(request.message)
        if not drive_query:
            return AgentResponse(
                request_id=request.request_id,
                agent_name=self.agent_name,
                status="requires_clarification",
                risk_level=RiskLevel.LOW,
                confidence=Confidence.MEDIUM,
                question=(
                    "Decime qué querés buscar en Google Drive.\n"
                    "- Nombre del archivo o carpeta\n"
                    "- Palabras clave como ventas, stock, proveedores\n"
                    "- Si querés, también podés decirme un período"
                ),
                result={"summary": "Falta la búsqueda para Drive."},
            )

        svc = GoogleMcpService(
            gateway=self._gateway,
            agent_name=self.agent_name,
            tenant_id=self._tenant_id,
            settings=get_settings(),
        )
        try:
            files = await svc.list_drive_files(query=drive_query, max_results=5)
            if not files:
                return AgentResponse(
                    request_id=request.request_id,
                    agent_name=self.agent_name,
                    status="success",
                    risk_level=RiskLevel.LOW,
                    confidence=Confidence.HIGH,
                    result={
                        "summary": f"No encontré archivos en Google Drive que coincidan con '{drive_query}'.",
                        "drive_query": drive_query,
                        "files_found": [],
                    },
                )

            readable_files: list[dict[str, str]] = []
            for file_item in files:
                file_id = str(file_item.get("id", "")).strip()
                mime_type = str(file_item.get("mime_type", "")).strip()
                if not file_id or mime_type == "application/vnd.google-apps.folder":
                    continue
                try:
                    file_data = await svc.read_drive_file(file_id=file_id)
                except Exception as exc:
                    logger.warning("agent_sync.drive_read_failed", file_id=file_id, error=str(exc))
                    continue

                preview = (
                    file_data.get("content_preview")
                    or file_data.get("raw_text_preview")
                    or ""
                )
                readable_files.append(
                    {
                        "id": file_id,
                        "name": str(file_item.get("name", "Archivo")),
                        "mime_type": mime_type,
                        "preview": str(preview).strip()[:500],
                    }
                )
                if len(readable_files) >= 2:
                    break

            return AgentResponse(
                request_id=request.request_id,
                agent_name=self.agent_name,
                status="success",
                risk_level=RiskLevel.LOW,
                confidence=Confidence.HIGH,
                result={
                    "summary": self._build_drive_summary(drive_query, files, readable_files),
                    "drive_query": drive_query,
                    "files_found": files,
                    "drive_reads": readable_files,
                },
            )
        except McpToolAuthError:
            return AgentResponse(
                request_id=request.request_id,
                agent_name=self.agent_name,
                status="requires_google_auth",
                risk_level=RiskLevel.LOW,
                confidence=Confidence.HIGH,
                result={
                    "action_type": ActionType.SYNC_TO_GOOGLE,
                    "mode": "informational",
                    "message": "Necesito que conectes Google para poder leer tus archivos de Drive.",
                },
            )

    def _extract_drive_query(self, message: str) -> str:
        stop_words = {
            "google", "drive", "buscar", "busca", "buscá", "leer", "lee",
            "analizar", "analiza", "archivo", "archivos", "carpeta", "carpetas",
            "de", "en", "del", "la", "los", "las", "mis", "mi",
        }
        normalized = "".join(
            char.lower() if char.isalnum() or char in {" ", ".", "-", "_"} else " "
            for char in message
        )
        tokens = [t for t in normalized.split() if len(t) > 2 and t not in stop_words]
        return " ".join(tokens)[:120].strip()

    def _build_drive_summary(
        self,
        query: str,
        files: list[dict[str, object]],
        readable_files: list[dict[str, str]],
    ) -> str:
        top_names = ", ".join(str(f.get("name", "archivo")) for f in files[:3])
        if readable_files:
            previews = " ".join(
                f"{item['name']}: {item['preview']}"
                for item in readable_files
                if item.get("preview")
            ).strip()
            if previews:
                return (
                    f"Encontré archivos en Google Drive para '{query}': {top_names}. "
                    f"Ya pude leer contenido útil para seguir trabajando. {previews[:700]}"
                )
        return f"Encontré archivos en Google Drive para '{query}': {top_names}."
