"""GoogleCalendarClient — wrapper mínimo sobre Google Calendar API v3."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

import httpx

from app.integrations.google_workspace.exceptions import InsufficientScopeError

_CALENDAR_BASE = "https://www.googleapis.com/calendar/v3"


class GoogleCalendarClient:
    """Cliente REST para Google Calendar API v3.

    Operaciones:
      - create_event(summary, start, end, description, attendees) → dict con id
      - list_events(time_min, time_max, max_results)               → list[dict]

    Scope mínimo requerido:
      - https://www.googleapis.com/auth/calendar  (create + list)
      - https://www.googleapis.com/auth/calendar.readonly (solo list)
    """

    def __init__(
        self,
        access_token: str,
        http_client: httpx.AsyncClient | None = None,
        calendar_id: str = "primary",
    ) -> None:
        self._token = access_token
        self._http = http_client
        self._calendar_id = calendar_id

    async def create_event(
        self,
        summary: str,
        start: str,
        end: str,
        description: str = "",
        attendees: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        """Crea un evento en el calendario primario.

        Args:
            summary:     Título del evento.
            start:       ISO 8601 datetime con timezone (e.g. "2026-05-01T09:00:00+00:00").
            end:         ISO 8601 datetime con timezone.
            description: Descripción opcional.
            attendees:   Lista de dicts {"email": "..."}.

        Returns:
            dict con al menos "id" y "htmlLink" del evento creado.
        """
        body: dict[str, Any] = {
            "summary": summary,
            "description": description,
            "start": {"dateTime": start, "timeZone": "UTC"},
            "end": {"dateTime": end, "timeZone": "UTC"},
        }
        if attendees:
            body["attendees"] = attendees

        async with self._http_context() as http:
            resp = await http.post(
                f"{_CALENDAR_BASE}/calendars/{self._calendar_id}/events",
                headers=self._auth_headers(),
                json=body,
            )
        self._raise_for_status(resp)
        return resp.json()  # type: ignore[no-any-return]

    async def list_events(
        self,
        time_min: str,
        time_max: str,
        max_results: int = 20,
    ) -> list[dict[str, Any]]:
        """Lista eventos en el rango de tiempo dado.

        Args:
            time_min:    RFC 3339 datetime — inicio del rango.
            time_max:    RFC 3339 datetime — fin del rango.
            max_results: Máximo de eventos a retornar (default 20).

        Returns:
            Lista de dicts de eventos crudos (summary, start, end, description, id).
        """
        data = await self._get(
            f"/calendars/{self._calendar_id}/events",
            params={
                "timeMin": time_min,
                "timeMax": time_max,
                "maxResults": str(max_results),
                "singleEvents": "true",
                "orderBy": "startTime",
            },
        )
        return data.get("items", [])  # type: ignore[no-any-return]

    # ── HTTP helpers ──────────────────────────────────────────────────────────

    async def _get(self, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        async with self._http_context() as http:
            resp = await http.get(
                f"{_CALENDAR_BASE}{path}",
                headers=self._auth_headers(),
                params=params or {},
            )
        self._raise_for_status(resp)
        return resp.json()  # type: ignore[no-any-return]

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}

    @staticmethod
    def _raise_for_status(resp: httpx.Response) -> None:
        if resp.status_code in (401, 403):
            try:
                reason = resp.json().get("error", {}).get("status", "")
            except Exception:
                reason = ""
            if resp.status_code == 401 or reason in {"PERMISSION_DENIED", "UNAUTHENTICATED"}:
                raise InsufficientScopeError(
                    f"Calendar {resp.status_code} insufficient scope"
                )
        resp.raise_for_status()

    @asynccontextmanager
    async def _http_context(self) -> AsyncGenerator[httpx.AsyncClient, None]:
        if self._http is not None:
            yield self._http
        else:
            async with httpx.AsyncClient(timeout=15.0) as client:
                yield client
