"""GoogleMcpService — wrappers semánticos sobre McpToolGateway para Google APIs.

Responsabilidades:
  1. Aplicar allowlist por agente (McpToolNotAllowedError si no permitido)
  2. Validar argumentos con Pydantic antes de invocar el gateway
  3. Respetar feature flag ENABLE_GOOGLE_MCP_TOOLS
  4. Traducir contratos de dominio → contratos de herramienta MCP
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, cast

from pydantic import BaseModel, Field

from app.application.ports.mcp_gateway import McpToolGateway
from app.integrations.mcp.exceptions import McpToolNotAllowedError
from app.observability.logger import get_logger

logger = get_logger(__name__)

# Allowlist de herramientas por agente. En código fuente — no en DB.
AGENT_TOOL_ALLOWLIST: dict[str, frozenset[str]] = {
    "agent_supplier": frozenset(
        {
            "google.gmail.list_messages",
            "google.gmail.get_message",
            "google.gmail.create_draft",
            "google.gmail.send_message",
            "google.gmail.reply_message",
            "google.drive.list_files",
            "google.drive.read_file",
            "google.drive.upload_file",
        }
    ),
    "agent_stock": frozenset(
        {
            "google.sheets.read_range",
            "google.sheets.append_rows",
            "google.drive.list_files",
            "google.drive.read_file",
            "google.drive.upload_file",
        }
    ),
    "agent_health": frozenset(
        {
            "google.drive.list_files",
            "google.drive.read_file",
            "google.sheets.append_rows",
            "google.docs.create_document",
            "google.docs.append_content",
        }
    ),
    # Stage 2: nuevos agentes
    "agent_income": frozenset(
        {
            "google.sheets.read_range",
            "google.sheets.append_rows",
            "google.drive.list_files",
            "google.drive.read_file",
            "google.drive.upload_file",
        }
    ),
    "agent_expense": frozenset(
        {
            "google.sheets.read_range",
            "google.sheets.append_rows",
            "google.drive.list_files",
            "google.drive.read_file",
        }
    ),
    "agent_google": frozenset(
        {
            "google.gmail.list_messages",
            "google.gmail.get_message",
            "google.gmail.create_draft",
            "google.gmail.send_message",
            "google.gmail.reply_message",
            "google.calendar.list_events",
            "google.calendar.create_event",
            "google.sheets.read_range",
            "google.sheets.append_rows",
            "google.drive.upload_file",
            "google.drive.list_files",
            "google.drive.read_file",
            "google.docs.create_document",
            "google.docs.append_content",
        }
    ),
    "agent_helper": frozenset(),
    "agent_ceo": frozenset(),
    "agent_stock_v2": frozenset(
        {
            "google.sheets.read_range",
        }
    ),
}

# ── Schemas Pydantic de argumentos ────────────────────────────────────────────


class _GmailListArgs(BaseModel):
    query: str
    max_results: int = Field(default=10, ge=1, le=50)


class _GmailGetArgs(BaseModel):
    message_id: str


class _GmailDraftArgs(BaseModel):
    to: list[str]
    subject: str = Field(max_length=500)
    body: str = Field(max_length=10_000)
    cc: list[str] = []


class _GmailSendArgs(BaseModel):
    to: list[str]
    subject: str = Field(max_length=500)
    body: str = Field(max_length=10_000)
    cc: list[str] = []


class _GmailReplyArgs(BaseModel):
    message_id: str
    body: str = Field(max_length=10_000)
    cc: list[str] = []


class _CalendarListArgs(BaseModel):
    time_min: str
    time_max: str
    max_results: int = Field(default=10, ge=1, le=50)


class _CalendarCreateArgs(BaseModel):
    summary: str = Field(max_length=500)
    start_datetime: str
    end_datetime: str
    description: str | None = Field(default=None, max_length=2000)
    attendees: list[str] = []


class _SheetsReadArgs(BaseModel):
    spreadsheet_id: str
    range: str = Field(max_length=100)


class _SheetsAppendArgs(BaseModel):
    spreadsheet_id: str
    range: str = Field(max_length=100)
    rows: list[list[Any]]


class _DriveUploadArgs(BaseModel):
    name: str = Field(max_length=255)
    content_base64: str
    mime_type: str
    folder_id: str | None = None


class _DriveListArgs(BaseModel):
    folder_id: str | None = None
    query: str | None = None
    max_results: int = Field(default=10, ge=1, le=50)


class _DriveReadArgs(BaseModel):
    file_id: str


class _DocsCreateArgs(BaseModel):
    title: str = Field(max_length=255)


class _DocsAppendArgs(BaseModel):
    document_id: str
    content: str = Field(max_length=50_000)


# ── Servicio ──────────────────────────────────────────────────────────────────


class GoogleMcpService:
    def __init__(
        self,
        gateway: McpToolGateway,
        agent_name: str,
        tenant_id: str,
        settings: Any,
    ) -> None:
        self._gateway = gateway
        self._agent_name = agent_name
        self._tenant_id = tenant_id
        self._enabled = settings.ENABLE_GOOGLE_MCP_TOOLS

    def _check_allowed(self, tool_name: str) -> None:
        allowed = AGENT_TOOL_ALLOWLIST.get(self._agent_name, frozenset())
        if tool_name not in allowed:
            raise McpToolNotAllowedError(
                f"Agente '{self._agent_name}' no tiene permiso para usar '{tool_name}'"
            )

    def _idempotency_key(self, tool_name: str, args: dict[str, Any]) -> str:
        raw = f"{self._tenant_id}:{tool_name}:{json.dumps(args, sort_keys=True)}"
        return hashlib.sha256(raw.encode()).hexdigest()[:32]

    async def _call(
        self, tool_name: str, args: dict[str, Any], *, idempotency: bool = False
    ) -> dict[str, Any]:
        self._check_allowed(tool_name)
        if not self._enabled:
            logger.info(
                "google_mcp_service.disabled",
                tool_name=tool_name,
                tenant_id=self._tenant_id,
            )
            return {
                "mode": "disabled",
                "message": "MCP tools deshabilitadas (ENABLE_GOOGLE_MCP_TOOLS=false)",
            }
        ikey = self._idempotency_key(tool_name, args) if idempotency else None
        result = await self._gateway.call_tool(
            tool_name, args, tenant_id=self._tenant_id, idempotency_key=ikey
        )
        return result.result

    # ── Gmail ─────────────────────────────────────────────────────────────────

    async def list_gmail_messages(self, query: str, max_results: int = 10) -> list[dict[str, Any]]:
        args = _GmailListArgs(query=query, max_results=max_results).model_dump()
        result = await self._call("google.gmail.list_messages", args)
        return cast("list[dict[str, Any]]", result.get("messages", []))

    async def get_gmail_message(self, message_id: str) -> dict[str, Any]:
        args = _GmailGetArgs(message_id=message_id).model_dump()
        return await self._call("google.gmail.get_message", args)

    async def create_gmail_draft(
        self,
        to: list[str],
        subject: str,
        body: str,
        cc: list[str] | None = None,
    ) -> dict[str, Any]:
        args = _GmailDraftArgs(to=to, subject=subject, body=body, cc=cc or []).model_dump()
        return await self._call("google.gmail.create_draft", args, idempotency=True)

    async def send_gmail_message(
        self,
        to: list[str],
        subject: str,
        body: str,
        cc: list[str] | None = None,
    ) -> dict[str, Any]:
        args = _GmailSendArgs(to=to, subject=subject, body=body, cc=cc or []).model_dump()
        return await self._call("google.gmail.send_message", args, idempotency=True)

    async def reply_gmail_message(
        self,
        message_id: str,
        body: str,
        cc: list[str] | None = None,
    ) -> dict[str, Any]:
        args = _GmailReplyArgs(message_id=message_id, body=body, cc=cc or []).model_dump()
        return await self._call("google.gmail.reply_message", args, idempotency=True)

    # ── Calendar ──────────────────────────────────────────────────────────────

    async def list_calendar_events(
        self, time_min: str, time_max: str, max_results: int = 10
    ) -> list[dict[str, Any]]:
        args = _CalendarListArgs(
            time_min=time_min, time_max=time_max, max_results=max_results
        ).model_dump()
        result = await self._call("google.calendar.list_events", args)
        return cast("list[dict[str, Any]]", result.get("events", []))

    async def create_calendar_event(
        self,
        summary: str,
        start: str,
        end: str,
        description: str | None = None,
        attendees: list[str] | None = None,
    ) -> dict[str, Any]:
        args = _CalendarCreateArgs(
            summary=summary,
            start_datetime=start,
            end_datetime=end,
            description=description,
            attendees=attendees or [],
        ).model_dump()
        return await self._call("google.calendar.create_event", args, idempotency=True)

    # ── Sheets ────────────────────────────────────────────────────────────────

    async def read_sheet_range(self, spreadsheet_id: str, range_name: str) -> list[list[Any]]:
        args = _SheetsReadArgs(spreadsheet_id=spreadsheet_id, range=range_name).model_dump()
        result = await self._call("google.sheets.read_range", args)
        return cast("list[list[Any]]", result.get("values", []))

    async def append_sheet_rows(
        self, spreadsheet_id: str, range_name: str, rows: list[list[Any]]
    ) -> dict[str, Any]:
        args = _SheetsAppendArgs(
            spreadsheet_id=spreadsheet_id, range=range_name, rows=rows
        ).model_dump()
        return await self._call("google.sheets.append_rows", args, idempotency=True)

    # ── Drive ─────────────────────────────────────────────────────────────────

    async def upload_drive_file(
        self,
        name: str,
        content_base64: str,
        mime_type: str,
        folder_id: str | None = None,
    ) -> dict[str, Any]:
        args = _DriveUploadArgs(
            name=name,
            content_base64=content_base64,
            mime_type=mime_type,
            folder_id=folder_id,
        ).model_dump()
        return await self._call("google.drive.upload_file", args, idempotency=True)

    async def list_drive_files(
        self,
        folder_id: str | None = None,
        query: str | None = None,
        max_results: int = 10,
    ) -> list[dict[str, Any]]:
        args = _DriveListArgs(
            folder_id=folder_id, query=query, max_results=max_results
        ).model_dump()
        result = await self._call("google.drive.list_files", args)
        return cast("list[dict[str, Any]]", result.get("files", []))

    async def read_drive_file(self, file_id: str) -> dict[str, Any]:
        args = _DriveReadArgs(file_id=file_id).model_dump()
        return await self._call("google.drive.read_file", args)

    # ── Docs ──────────────────────────────────────────────────────────────────

    async def create_doc(self, title: str) -> dict[str, Any]:
        args = _DocsCreateArgs(title=title).model_dump()
        return await self._call("google.docs.create_document", args, idempotency=True)

    async def append_doc_content(self, document_id: str, content: str) -> dict[str, Any]:
        args = _DocsAppendArgs(document_id=document_id, content=content).model_dump()
        return await self._call("google.docs.append_content", args)
