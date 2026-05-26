"""GoogleToolBroker — infraestructura de acceso a Google APIs.

Único punto de entrada para operaciones MCP de Google. Solo debe ser importado por:
  - app.application.agents.google.agent (AgentGoogle usa el broker en su core)
  - app.application.services.pending_action_service (ejecuta PendingActions vía broker)

Los agentes de dominio (income, expense, stock, health, supplier) NO importan este módulo.
Emiten PendingActions con ActionType externo; PendingActionService delega al broker.
"""

from __future__ import annotations

from typing import Any


class GoogleToolBroker:
    """Thin wrapper sobre GoogleMcpService con agent_name='agent_google'."""

    def __init__(self, *, user_id: str, tenant_id: str, settings: Any) -> None:
        from app.integrations.mcp.google_mcp_service import GoogleMcpService  # noqa: PLC0415
        from app.integrations.mcp.http_gateway import HttpMcpGateway  # noqa: PLC0415

        gateway = HttpMcpGateway(settings=settings, user_id=user_id)
        self._svc = GoogleMcpService(
            gateway=gateway,
            agent_name="agent_google",
            tenant_id=tenant_id,
            settings=settings,
        )

    # ── Gmail ─────────────────────────────────────────────────────────────────

    async def list_gmail_messages(self, query: str, max_results: int = 10) -> list[dict[str, Any]]:
        return await self._svc.list_gmail_messages(query=query, max_results=max_results)

    async def get_gmail_message(self, message_id: str) -> dict[str, Any]:
        return await self._svc.get_gmail_message(message_id=message_id)

    async def create_gmail_draft(
        self,
        to: list[str],
        subject: str,
        body: str,
        cc: list[str] | None = None,
    ) -> dict[str, Any]:
        return await self._svc.create_gmail_draft(to=to, subject=subject, body=body, cc=cc)

    async def send_gmail_message(
        self,
        to: list[str],
        subject: str,
        body: str,
        cc: list[str] | None = None,
    ) -> dict[str, Any]:
        return await self._svc.send_gmail_message(to=to, subject=subject, body=body, cc=cc)

    async def reply_gmail_message(
        self,
        message_id: str,
        body: str,
        cc: list[str] | None = None,
    ) -> dict[str, Any]:
        return await self._svc.reply_gmail_message(message_id=message_id, body=body, cc=cc)

    # ── Calendar ──────────────────────────────────────────────────────────────

    async def create_calendar_event(
        self,
        summary: str,
        start: str,
        end: str,
        description: str | None = None,
        attendees: list[str] | None = None,
    ) -> dict[str, Any]:
        return await self._svc.create_calendar_event(
            summary=summary,
            start=start,
            end=end,
            description=description,
            attendees=attendees,
        )

    # ── Sheets ────────────────────────────────────────────────────────────────

    async def read_sheet_range(self, spreadsheet_id: str, range_name: str) -> list[list[Any]]:
        return await self._svc.read_sheet_range(
            spreadsheet_id=spreadsheet_id, range_name=range_name
        )

    async def append_sheet_rows(
        self, spreadsheet_id: str, range_name: str, rows: list[list[Any]]
    ) -> dict[str, Any]:
        return await self._svc.append_sheet_rows(
            spreadsheet_id=spreadsheet_id, range_name=range_name, rows=rows
        )

    # ── Drive ─────────────────────────────────────────────────────────────────

    async def upload_drive_file(
        self,
        name: str,
        content_base64: str,
        mime_type: str,
        folder_id: str | None = None,
    ) -> dict[str, Any]:
        return await self._svc.upload_drive_file(
            name=name,
            content_base64=content_base64,
            mime_type=mime_type,
            folder_id=folder_id,
        )

    async def list_drive_files(
        self,
        folder_id: str | None = None,
        query: str | None = None,
        max_results: int = 10,
    ) -> list[dict[str, Any]]:
        return await self._svc.list_drive_files(
            folder_id=folder_id, query=query, max_results=max_results
        )

    async def read_drive_file(self, file_id: str) -> dict[str, Any]:
        return await self._svc.read_drive_file(file_id=file_id)

    # ── Docs ──────────────────────────────────────────────────────────────────

    async def create_doc(self, title: str) -> dict[str, Any]:
        return await self._svc.create_doc(title=title)

    async def append_doc_content(self, document_id: str, content: str) -> dict[str, Any]:
        return await self._svc.append_doc_content(document_id=document_id, content=content)
