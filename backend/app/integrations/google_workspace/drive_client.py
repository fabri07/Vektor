"""GoogleDriveClient — wrapper mínimo sobre Google Drive API v3."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

import httpx
from pydantic import BaseModel

from app.integrations.google_workspace.exceptions import InsufficientScopeError

_DRIVE_BASE = "https://www.googleapis.com/drive/v3"


class DriveFile(BaseModel):
    file_id: str
    name: str
    mime_type: str
    modified_time: str | None = None
    web_view_link: str | None = None


class GoogleDriveClient:
    """Cliente REST para descubrir archivos de Google Drive."""

    def __init__(
        self,
        access_token: str,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._token = access_token
        self._http = http_client

    async def list_files(
        self,
        query: str | None = None,
        max_results: int = 20,
    ) -> list[DriveFile]:
        default_query = (
            "trashed=false and ("
            "mimeType='application/vnd.google-apps.spreadsheet' or "
            "mimeType='text/csv' or "
            "mimeType='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'"
            ")"
        )
        data = await self._get(
            "/files",
            params={
                "q": query or default_query,
                "pageSize": str(max_results),
                "fields": "files(id,name,mimeType,modifiedTime,webViewLink)",
                "orderBy": "modifiedTime desc",
            },
        )
        return [
            DriveFile(
                file_id=str(item.get("id", "")),
                name=str(item.get("name", "")),
                mime_type=str(item.get("mimeType", "")),
                modified_time=item.get("modifiedTime"),
                web_view_link=item.get("webViewLink"),
            )
            for item in data.get("files", [])
        ]

    async def export_file(self, file_id: str, mime_type: str = "text/csv") -> bytes:
        async with self._http_context() as http:
            resp = await http.get(
                f"{_DRIVE_BASE}/files/{file_id}/export",
                headers=self._auth_headers(),
                params={"mimeType": mime_type},
            )
        self._raise_for_status(resp)
        return resp.content

    async def _get(self, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        async with self._http_context() as http:
            resp = await http.get(
                f"{_DRIVE_BASE}{path}",
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
                raise InsufficientScopeError(f"Drive {resp.status_code} insufficient scope")
        resp.raise_for_status()

    @asynccontextmanager
    async def _http_context(self) -> AsyncGenerator[httpx.AsyncClient, None]:
        if self._http is not None:
            yield self._http
        else:
            async with httpx.AsyncClient(timeout=15.0) as client:
                yield client
