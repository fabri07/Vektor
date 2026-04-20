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

    async def upload_file(
        self,
        name: str,
        content: bytes,
        mime_type: str = "application/octet-stream",
        folder_name: str = "Véktor",
    ) -> dict[str, Any]:
        """Sube un archivo a Google Drive usando multipart upload.

        Crea la carpeta ``folder_name`` si no existe, y sube el archivo dentro.
        Retorna el dict de metadata del archivo (id, name, webViewLink, etc.).
        """
        folder_id = await self._get_or_create_folder(folder_name)

        metadata = {"name": name, "parents": [folder_id]}

        import json as _json  # noqa: PLC0415

        boundary = "vektorbound"
        body = (
            f"--{boundary}\r\n"
            f"Content-Type: application/json; charset=UTF-8\r\n\r\n"
            f"{_json.dumps(metadata)}\r\n"
            f"--{boundary}\r\n"
            f"Content-Type: {mime_type}\r\n\r\n"
        ).encode() + content + f"\r\n--{boundary}--".encode()

        async with self._http_context() as http:
            resp = await http.post(
                "https://www.googleapis.com/upload/drive/v3/files",
                headers={
                    **self._auth_headers(),
                    "Content-Type": f"multipart/related; boundary={boundary}",
                },
                params={"uploadType": "multipart", "fields": "id,name,webViewLink,mimeType"},
                content=body,
            )
        self._raise_for_status(resp)
        return resp.json()  # type: ignore[no-any-return]

    async def _get_or_create_folder(self, folder_name: str) -> str:
        """Retorna el id de la carpeta, creándola si no existe."""
        data = await self._get(
            "/files",
            params={
                "q": (
                    f"name='{folder_name}' and "
                    "mimeType='application/vnd.google-apps.folder' and "
                    "trashed=false"
                ),
                "fields": "files(id)",
                "pageSize": "1",
            },
        )
        files = data.get("files", [])
        if files:
            return str(files[0]["id"])

        # Crear carpeta
        async with self._http_context() as http:
            resp = await http.post(
                f"{_DRIVE_BASE}/files",
                headers=self._auth_headers(),
                json={
                    "name": folder_name,
                    "mimeType": "application/vnd.google-apps.folder",
                },
            )
        self._raise_for_status(resp)
        return str(resp.json()["id"])

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
