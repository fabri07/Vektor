"""GmailClient — wrapper sobre la Gmail REST API v1.

Solo se exponen las operaciones necesarias para AgentSupplier:
  - list_messages(query, max_results)   → list[GmailMessageSummary]
  - get_message(message_id)             → GmailMessage
  - create_draft(to, subject, body)     → draft_id: str

Scope mínimo requerido:
  - gmail.readonly  → list_messages + get_message
  - gmail.compose   → create_draft

El access_token debe estar vigente (gestionado por TokenManager).
El cliente no refresca tokens ni maneja auth — eso es responsabilidad del gateway.

InsufficientScopeError se lanza cuando:
  - HTTP 401 con error=insufficient_scope
  - HTTP 403 con error.errors[].reason=insufficientPermissions
El gateway la captura y la convierte en WorkspaceTokenError(reason="insufficient_scope").
"""

from __future__ import annotations

import base64
import email.mime.text
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

import httpx
from pydantic import BaseModel

from app.integrations.google_workspace.exceptions import InsufficientScopeError

_GMAIL_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"


# ── Schemas de respuesta ──────────────────────────────────────────────────────

class GmailMessageSummary(BaseModel):
    """Metadata de un mensaje (sin body). Retornada por list_messages."""

    message_id: str
    thread_id: str
    subject: str | None = None
    from_: str | None = None
    snippet: str | None = None
    date: str | None = None
    labels: list[str] = []


class GmailMessage(GmailMessageSummary):
    """Mensaje completo con body. Retornado por get_message."""

    body_text: str | None = None
    body_html: str | None = None


# ── Cliente ───────────────────────────────────────────────────────────────────

class GmailClient:
    """Cliente REST sobre la Gmail API.

    Args:
        access_token: Token OAuth vigente (texto plano).
        http_client:  AsyncClient inyectado en tests; None crea uno en producción.
    """

    def __init__(
        self,
        access_token: str,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._token = access_token
        self._http = http_client

    # ── Operaciones públicas ──────────────────────────────────────────────────

    async def list_messages(
        self,
        query: str = "",
        max_results: int = 20,
    ) -> list[GmailMessageSummary]:
        """Lista mensajes. Cada ítem trae solo metadata (sin body)."""
        data = await self._get(
            "/messages",
            params={"q": query, "maxResults": str(max_results)},
        )
        raw_list: list[dict[str, Any]] = data.get("messages", [])

        results: list[GmailMessageSummary] = []
        for item in raw_list:
            meta = await self._get(
                f"/messages/{item['id']}",
                params={
                    "format": "metadata",
                    "metadataHeaders": "From,Subject,Date",
                },
            )
            results.append(self._parse_summary(meta))
        return results

    async def get_message(self, message_id: str) -> GmailMessage:
        """Mensaje completo (body incluido)."""
        data = await self._get(f"/messages/{message_id}", params={"format": "full"})
        return self._parse_full(data)

    async def create_draft(self, to: str, subject: str, body: str) -> str:
        """Crea un draft en Gmail. Retorna el draft_id (string)."""
        mime = email.mime.text.MIMEText(body, "plain", "utf-8")
        mime["To"] = to
        mime["Subject"] = subject
        raw = base64.urlsafe_b64encode(mime.as_bytes()).decode()

        async with self._http_context() as http:
            resp = await http.post(
                f"{_GMAIL_BASE}/drafts",
                headers=self._auth_headers(),
                json={"message": {"raw": raw}},
            )
        self._raise_for_status(resp)
        return str(resp.json()["id"])

    # ── HTTP helpers ──────────────────────────────────────────────────────────

    async def _get(self, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        async with self._http_context() as http:
            resp = await http.get(
                f"{_GMAIL_BASE}{path}",
                headers=self._auth_headers(),
                params=params or {},
            )
        self._raise_for_status(resp)
        return resp.json()  # type: ignore[no-any-return]

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}

    @staticmethod
    def _raise_for_status(resp: httpx.Response) -> None:
        """Convierte errores HTTP en excepciones tipadas."""
        if resp.status_code == 401:
            raise InsufficientScopeError(
                f"Gmail 401 — insufficient_scope or token expired (body={resp.text[:200]})"
            )
        if resp.status_code == 403:
            try:
                errors = resp.json().get("error", {}).get("errors", [{}])
            except Exception:
                errors = [{}]
            reasons = {e.get("reason", "") for e in errors}
            if "insufficientPermissions" in reasons or "forbidden" in reasons:
                raise InsufficientScopeError(f"Gmail 403 insufficientPermissions")
        resp.raise_for_status()

    @asynccontextmanager
    async def _http_context(self) -> AsyncGenerator[httpx.AsyncClient, None]:
        if self._http is not None:
            yield self._http
        else:
            async with httpx.AsyncClient(timeout=15.0) as client:
                yield client

    # ── Parsers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _parse_summary(data: dict[str, Any]) -> GmailMessageSummary:
        headers: dict[str, str] = {
            h["name"].lower(): h["value"]
            for h in data.get("payload", {}).get("headers", [])
        }
        return GmailMessageSummary(
            message_id=data["id"],
            thread_id=data.get("threadId", ""),
            subject=headers.get("subject"),
            from_=headers.get("from"),
            snippet=data.get("snippet"),
            date=headers.get("date"),
            labels=data.get("labelIds", []),
        )

    @classmethod
    def _parse_full(cls, data: dict[str, Any]) -> GmailMessage:
        summary = cls._parse_summary(data)
        body_text: str | None = None
        body_html: str | None = None

        def _extract(payload: dict[str, Any]) -> None:
            nonlocal body_text, body_html
            mime_type = payload.get("mimeType", "")
            raw_data: str = payload.get("body", {}).get("data", "")
            if raw_data:
                # urlsafe base64 — pad to multiple of 4
                decoded = base64.urlsafe_b64decode(raw_data + "==").decode("utf-8", errors="replace")
                if mime_type == "text/plain" and body_text is None:
                    body_text = decoded
                elif mime_type == "text/html" and body_html is None:
                    body_html = decoded
            for part in payload.get("parts", []):
                _extract(part)

        _extract(data.get("payload", {}))
        return GmailMessage(**summary.model_dump(), body_text=body_text, body_html=body_html)
