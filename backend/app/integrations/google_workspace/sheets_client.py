"""GoogleSheetsClient — wrapper mínimo sobre Google Sheets API v4."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

import httpx
from pydantic import BaseModel

from app.integrations.google_workspace.exceptions import InsufficientScopeError

_SHEETS_BASE = "https://sheets.googleapis.com/v4/spreadsheets"


class SheetInfo(BaseModel):
    sheet_id: int
    title: str
    row_count: int | None = None
    column_count: int | None = None


class SpreadsheetInfo(BaseModel):
    spreadsheet_id: str
    title: str | None = None
    sheets: list[SheetInfo] = []


class SheetValues(BaseModel):
    spreadsheet_id: str
    range: str
    values: list[list[str]] = []


class GoogleSheetsClient:
    """Cliente REST para leer metadata y valores de Google Sheets."""

    def __init__(
        self,
        access_token: str,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._token = access_token
        self._http = http_client

    async def get_spreadsheet(self, spreadsheet_id: str) -> SpreadsheetInfo:
        data = await self._get(
            f"/{spreadsheet_id}",
            params={"fields": "spreadsheetId,properties.title,sheets(properties(sheetId,title,gridProperties))"},
        )
        sheets: list[SheetInfo] = []
        for item in data.get("sheets", []):
            props = item.get("properties", {})
            grid = props.get("gridProperties", {})
            sheets.append(
                SheetInfo(
                    sheet_id=int(props.get("sheetId", 0)),
                    title=str(props.get("title", "")),
                    row_count=grid.get("rowCount"),
                    column_count=grid.get("columnCount"),
                )
            )
        return SpreadsheetInfo(
            spreadsheet_id=str(data.get("spreadsheetId", spreadsheet_id)),
            title=data.get("properties", {}).get("title"),
            sheets=sheets,
        )

    async def read_values(
        self,
        spreadsheet_id: str,
        range_name: str = "A1:Z100",
    ) -> SheetValues:
        data = await self._get(f"/{spreadsheet_id}/values/{range_name}")
        values = [[str(cell) for cell in row] for row in data.get("values", [])]
        return SheetValues(
            spreadsheet_id=spreadsheet_id,
            range=str(data.get("range", range_name)),
            values=values,
        )

    async def _get(self, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        async with self._http_context() as http:
            resp = await http.get(
                f"{_SHEETS_BASE}{path}",
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
                raise InsufficientScopeError(f"Sheets {resp.status_code} insufficient scope")
        resp.raise_for_status()

    @asynccontextmanager
    async def _http_context(self) -> AsyncGenerator[httpx.AsyncClient, None]:
        if self._http is not None:
            yield self._http
        else:
            async with httpx.AsyncClient(timeout=15.0) as client:
                yield client
