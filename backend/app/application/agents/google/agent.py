"""AgentGoogle — ejecutor de acciones Google (Stage 5d: absorbe AgentCalendar + AgentSync).

Responsabilidades:
- CREATE_CALENDAR_EVENT: extrae datos del evento via LLM → PendingAction
- SYNC_TO_GOOGLE: clasifica tipo de sync → PendingAction o lectura directa via broker
- UPLOAD_TO_DRIVE / CREATE_GOOGLE_DOC / APPEND_TO_SHEET: PendingAction → broker

AgentGoogle es el ÚNICO agente que importa GoogleToolBroker directamente.
Los agentes de dominio (income, expense, stock, health, supplier) NO importan el broker;
emiten PendingActions con ActionType externo y PendingActionService delega al broker.
"""

from __future__ import annotations

import base64
import re
from typing import TYPE_CHECKING, Any

import anthropic

from app.application.agents.base import BaseAgent
from app.application.agents.google.tool_broker import GoogleToolBroker
from app.application.agents.shared.json_parse import parse_llm_json
from app.application.agents.shared.llm_safe import call_llm
from app.application.agents.shared.schemas import (
    ActionType,
    AgentRequest,
    AgentResponse,
    Confidence,
    LLMCall,
    RiskLevel,
)
from app.application.security.prompt_defense import wrap_user_input
from app.integrations.anthropic_client import get_anthropic_async_client
from app.integrations.mcp.exceptions import McpToolAuthError
from app.observability.logger import get_logger

if TYPE_CHECKING:
    from app.application.ports.mcp_gateway import McpToolGateway

logger = get_logger(__name__)

# ── Regex para extraer IDs de URLs Google ─────────────────────────────────────
_SHEETS_ID_RE = re.compile(r"/spreadsheets/d/([a-zA-Z0-9_-]+)")
_BARE_ID_RE = re.compile(r"(?<![/\w])([a-zA-Z0-9_-]{20,})(?![/\w])")

# ── Sync types + keyword mapping ──────────────────────────────────────────────
_SYNC_TYPES: dict[str, dict[str, Any]] = {
    "export_sales_to_sheets": {
        "keywords": (
            "exportar ventas",
            "ventas a sheets",
            "exportar a google sheets",
            "subir ventas",
        ),
        "tool": "google.sheets.append_rows",
        "summary": "Exportar ventas a Google Sheets.",
        "needs_sheet": True,
    },
    "export_report_to_docs": {
        "keywords": ("exportar reporte", "reporte a docs", "informe a google docs", "generar doc"),
        "tool": "google.docs.create_document",
        "summary": "Exportar reporte a Google Docs.",
        "needs_sheet": False,
    },
    "import_from_sheets": {
        "keywords": (
            "importar desde sheets",
            "importar de google",
            "traer datos de sheets",
            "leer sheets",
        ),
        "tool": "google.sheets.read_range",
        "summary": "Importar datos desde Google Sheets.",
        "needs_sheet": True,
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
    },
}

# ── Calendar query keywords ────────────────────────────────────────────────────
_CALENDAR_QUERY_KW = (
    "ver agenda",
    "mis eventos",
    "qué tengo",
    "que tengo",
    "próximos eventos",
    "proximos eventos",
    "consultar calendario",
    "ver calendario",
    "agenda de",
)

# ── ActionTypes despachados via PendingAction → broker ────────────────────────
_BROKER_ACTION_TYPES = frozenset(
    {
        ActionType.UPLOAD_TO_DRIVE,
        ActionType.CREATE_GOOGLE_DOC,
        ActionType.APPEND_TO_SHEET,
    }
)

_BROKER_ACTION_LABELS: dict[ActionType, str] = {
    ActionType.UPLOAD_TO_DRIVE: "Subir archivo a Google Drive",
    ActionType.CREATE_GOOGLE_DOC: "Crear documento en Google Docs",
    ActionType.APPEND_TO_SHEET: "Agregar datos a Google Sheets",
}

# Palabras irrelevantes para la query de Drive
_DRIVE_STOP_WORDS = {
    "google",
    "drive",
    "buscar",
    "busca",
    "buscá",
    "leer",
    "lee",
    "analizar",
    "analiza",
    "archivo",
    "archivos",
    "carpeta",
    "carpetas",
    "de",
    "en",
    "del",
    "la",
    "los",
    "las",
    "mis",
    "mi",
}


class AgentGoogle(BaseAgent):
    agent_name = "agent_google"

    def __init__(
        self,
        gateway: McpToolGateway | None = None,
        tenant_id: str | None = None,
        user_id: str | None = None,
    ) -> None:
        self._gateway = gateway
        self._tenant_id = tenant_id
        self._user_id = user_id
        self._client: Any | None = None

    @property
    def client(self) -> Any:
        if self._client is None:
            self._client = get_anthropic_async_client(anthropic.AsyncAnthropic)
        return self._client

    @client.setter
    def client(self, value: Any) -> None:
        self._client = value

    def _get_broker(self, request: AgentRequest) -> GoogleToolBroker:
        from app.config.settings import get_settings  # noqa: PLC0415

        return GoogleToolBroker(
            user_id=request.user_id,
            tenant_id=self._tenant_id or "",
            settings=get_settings(),
        )

    def make_broker(self, settings: Any) -> GoogleToolBroker:
        """Crea un GoogleToolBroker con credenciales del agente."""
        return GoogleToolBroker(
            user_id=self._user_id or "",
            tenant_id=self._tenant_id or "",
            settings=settings,
        )

    # ── Router principal ──────────────────────────────────────────────────────

    async def process(
        self,
        request: AgentRequest,
        task: Any | None = None,
    ) -> AgentResponse:
        action_type = getattr(task, "action_type", None) if task else None

        if action_type in _BROKER_ACTION_TYPES:
            return self._pending_for_broker_action(request, action_type, task)

        if action_type == ActionType.CREATE_CALENDAR_EVENT:
            return await self._handle_calendar_event(request)

        # Default: SYNC_TO_GOOGLE y cualquier otro
        return await self._handle_sync(request)

    # ── UPLOAD_TO_DRIVE / CREATE_GOOGLE_DOC / APPEND_TO_SHEET ────────────────

    def _pending_for_broker_action(
        self,
        request: AgentRequest,
        action_type: ActionType,
        task: Any | None,
    ) -> AgentResponse:
        """Emite requires_approval para que PendingActionService ejecute via broker.

        Para UPLOAD_TO_DRIVE: lee upstream_outputs del DAG para obtener el contenido
        real del informe de salud (generado por AgentHealth en el task previo).

        Stage 4: export como texto plano (narrativa + score).
        Stage 5a: AgentHealth generará PDF con reportlab + artifact R2;
                  el payload incluirá blob_id en lugar de content_base64 inline.
        """
        entities = dict(task.entities) if task else {}

        if action_type == ActionType.UPLOAD_TO_DRIVE and not entities.get("content_base64"):
            upstream_outputs = request.context.get("upstream_outputs", {})
            if upstream_outputs:
                upstream_result: dict[str, Any] = next(iter(upstream_outputs.values()), {})
                narrative: str = upstream_result.get("summary", "")
                health_score = upstream_result.get("health_score")
                if narrative:
                    lines: list[str] = []
                    if health_score is not None:
                        lines.append(f"Score de salud: {health_score}/100\n")
                    lines.append(narrative)
                    entities["content_base64"] = base64.b64encode(
                        "\n".join(lines).encode()
                    ).decode()
                    entities.setdefault("filename", "informe_salud_vektor.txt")
                    entities.setdefault("mime_type", "text/plain")

        payload = {**entities, "mode": "mcp", "raw_message": request.message}
        return AgentResponse(
            request_id=request.request_id,
            agent_name=self.agent_name,
            status="requires_approval",
            risk_level=RiskLevel.MEDIUM,
            confidence=Confidence.HIGH,
            requires_approval=True,
            result={
                "action_type": action_type,
                "summary": _BROKER_ACTION_LABELS.get(action_type, "Acción Google"),
                "mode": "mcp",
                "payload": payload,
            },
        )

    # ── CREATE_CALENDAR_EVENT ─────────────────────────────────────────────────

    async def _handle_calendar_event(self, request: AgentRequest) -> AgentResponse:
        if self._gateway is None:
            return AgentResponse(
                request_id=request.request_id,
                agent_name=self.agent_name,
                status="requires_google_auth",
                risk_level=RiskLevel.LOW,
                confidence=Confidence.HIGH,
                result={
                    "action_type": ActionType.CREATE_CALENDAR_EVENT,
                    "mode": "informational",
                    "message": "Necesito que la integración de Google Calendar esté disponible.",
                },
            )

        message = request.message.lower()
        if any(kw in message for kw in _CALENDAR_QUERY_KW):
            return AgentResponse(
                request_id=request.request_id,
                agent_name=self.agent_name,
                status="requires_approval",
                risk_level=RiskLevel.LOW,
                confidence=Confidence.HIGH,
                requires_approval=True,
                result={
                    "action_type": ActionType.CREATE_CALENDAR_EVENT,
                    "summary": "Consultar eventos próximos en Google Calendar.",
                    "mode": "mcp",
                    "payload": {"query_only": True, "mode": "mcp", "raw_message": request.message},
                },
            )

        extracted, cal_call = await self._extract_calendar_event_data(request.message)
        usage_calls = [cal_call] if cal_call else []

        if not extracted.get("has_enough_info"):
            from app.application.agents.shared.schemas import UsageSummary  # noqa: PLC0415

            return AgentResponse(
                request_id=request.request_id,
                agent_name=self.agent_name,
                status="requires_clarification",
                risk_level=RiskLevel.LOW,
                confidence=Confidence.LOW,
                question=(
                    "Para crear el evento necesito:\n"
                    "- Título del evento\n"
                    "- Fecha y hora de inicio\n"
                    "- Duración o hora de fin\n"
                    "¿Podés darme esos datos?"
                ),
                result={"summary": "Faltan datos del evento de calendario."},
                usage=UsageSummary(calls=usage_calls),
            )

        from app.application.agents.shared.schemas import UsageSummary  # noqa: PLC0415

        summary_text = extracted.get("summary", "Evento")
        start = extracted.get("start_datetime", "")
        end = extracted.get("end_datetime", "")
        display = f"Crear evento: {summary_text}\nFecha: {start}"
        if end:
            display += f" → {end}"
        return AgentResponse(
            request_id=request.request_id,
            agent_name=self.agent_name,
            status="requires_approval",
            risk_level=RiskLevel.MEDIUM,
            confidence=Confidence.HIGH,
            requires_approval=True,
            result={
                "action_type": ActionType.CREATE_CALENDAR_EVENT,
                "summary": display,
                "mode": "mcp",
                "payload": {
                    "summary": summary_text,
                    "start_datetime": start,
                    "end_datetime": end,
                    "attendees": extracted.get("attendees", []),
                    "description": extracted.get("description", ""),
                    "mode": "mcp",
                    "raw_message": request.message,
                },
            },
            usage=UsageSummary(calls=usage_calls),
        )

    async def _extract_calendar_event_data(
        self, message: str
    ) -> tuple[dict[str, Any], LLMCall | None]:
        from datetime import date  # noqa: PLC0415

        today = date.today().isoformat()

        system = (
            f"Hoy es {today}. Sos el asistente de Véktor.\n"
            "Extraé datos de un evento de Google Calendar desde el mensaje del usuario.\n\n"
            "Retorná SOLO un JSON:\n"
            "{\n"
            '  "has_enough_info": true|false,\n'
            '  "summary": "título del evento",\n'
            '  "start_datetime": "ISO 8601, ej: 2026-04-28T10:00:00",\n'
            '  "end_datetime": "ISO 8601 o null",\n'
            '  "attendees": ["email@example.com"] o [],\n'
            '  "description": "descripción adicional o null"\n'
            "}\n\n"
            "has_enough_info=true si hay al menos título y fecha/hora.\n"
            "Si falta la hora, usá 09:00:00. Si falta la duración, asumí 1 hora."
        )
        raw, llm_call = await call_llm(
            client=self.client,
            source="agent_google",
            model="claude-sonnet-4-6",
            system=system,
            messages=[{"role": "user", "content": wrap_user_input(message)}],
            max_tokens=600,
        )
        if raw is None:
            return {"has_enough_info": False}, None
        parsed = parse_llm_json(raw)
        if parsed is None:
            logger.warning("agent_google.calendar_extract_json_failed", raw=raw[:200])
            return {"has_enough_info": False}, llm_call
        return parsed, llm_call

    # ── SYNC_TO_GOOGLE ────────────────────────────────────────────────────────

    async def _handle_sync(self, request: AgentRequest) -> AgentResponse:
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
                    "message": "Necesito que la integración de Google esté disponible.",
                },
            )

        message_lower = request.message.lower()
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
                    "summary": f"{cfg['summary']} Archivo: {spreadsheet_id[:20]}...",
                    "mode": "mcp",
                    "payload": payload,
                },
            )

        payload = {"sync_type": sync_type, "mode": "mcp", "raw_message": request.message}
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
                    "- Palabras clave como ventas, stock, proveedores"
                ),
                result={"summary": "Falta la búsqueda para Drive."},
            )

        broker = self._get_broker(request)
        try:
            files = await broker.list_drive_files(query=drive_query, max_results=5)
            if not files:
                return AgentResponse(
                    request_id=request.request_id,
                    agent_name=self.agent_name,
                    status="success",
                    risk_level=RiskLevel.LOW,
                    confidence=Confidence.HIGH,
                    result={
                        "summary": f"No encontré archivos en Google Drive para '{drive_query}'.",
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
                    file_data = await broker.read_drive_file(file_id=file_id)
                except Exception as exc:
                    logger.warning(
                        "agent_google.drive_read_failed", file_id=file_id, error=str(exc)
                    )
                    continue
                preview = (
                    file_data.get("content_preview") or file_data.get("raw_text_preview") or ""
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
                    "message": "Necesito que conectes Google para leer tus archivos de Drive.",
                },
            )

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _classify_sync_type(self, message: str) -> str | None:
        for sync_type, cfg in _SYNC_TYPES.items():
            if any(kw in message for kw in cfg["keywords"]):
                return sync_type
        return None

    def _extract_spreadsheet_id(self, message: str) -> str | None:
        m = _SHEETS_ID_RE.search(message)
        if m:
            return m.group(1)
        m = _BARE_ID_RE.search(message)
        if m:
            return m.group(1)
        return None

    def _extract_drive_query(self, message: str) -> str:
        normalized = "".join(
            c.lower() if c.isalnum() or c in {" ", ".", "-", "_"} else " " for c in message
        )
        tokens = [t for t in normalized.split() if len(t) > 2 and t not in _DRIVE_STOP_WORDS]
        return " ".join(tokens)[:120].strip()

    def _build_drive_summary(
        self,
        query: str,
        files: list[dict[str, Any]],
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
                    f"Ya pude leer contenido útil. {previews[:700]}"
                )
        return f"Encontré archivos en Google Drive para '{query}': {top_names}."
