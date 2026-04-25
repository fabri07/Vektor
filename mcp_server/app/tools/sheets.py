from __future__ import annotations

from typing import Any

import httpx
from fastapi import HTTPException, status

from app.auth.service import get_valid_access_token
from app.security import RequestContext

SHEETS_BASE = "https://sheets.googleapis.com/v4/spreadsheets"


async def read_range(
    *,
    session: Any,
    ctx: RequestContext,
    spreadsheet_id: str,
    range_: str,
) -> dict[str, Any]:
    access_token, _ = await get_valid_access_token(session, ctx)

    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(
            f"{SHEETS_BASE}/{spreadsheet_id}/values/{range_}",
            headers={"Authorization": f"Bearer {access_token}"},
        )

    if resp.status_code == status.HTTP_401_UNAUTHORIZED:
        raise HTTPException(status_code=401, detail="refresh_failed")
    if resp.status_code == 403:
        raise HTTPException(status_code=403, detail="insufficient_scope")
    if resp.status_code == 404:
        raise HTTPException(status_code=404, detail="spreadsheet_not_found")
    if resp.status_code >= 400:
        raise HTTPException(status_code=400, detail="sheets_read_failed")

    payload = resp.json()
    values = payload.get("values", [])
    return {
        "spreadsheet_id": spreadsheet_id,
        "range": payload.get("range", range_),
        "values": values,
        "row_count": len(values),
    }


async def append_rows(
    *,
    session: Any,
    ctx: RequestContext,
    spreadsheet_id: str,
    range_: str,
    values: list[list[Any]],
) -> dict[str, Any]:
    access_token, _ = await get_valid_access_token(session, ctx)

    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.post(
            f"{SHEETS_BASE}/{spreadsheet_id}/values/{range_}:append",
            params={"valueInputOption": "USER_ENTERED", "insertDataOption": "INSERT_ROWS"},
            json={"values": values},
            headers={"Authorization": f"Bearer {access_token}"},
        )

    if resp.status_code == 401:
        raise HTTPException(status_code=401, detail="refresh_failed")
    if resp.status_code == 403:
        raise HTTPException(status_code=403, detail="insufficient_scope")
    if resp.status_code == 404:
        raise HTTPException(status_code=404, detail="spreadsheet_not_found")
    if resp.status_code >= 400:
        raise HTTPException(status_code=400, detail="sheets_append_failed")

    payload = resp.json()
    updates = payload.get("updates", {})
    return {
        "spreadsheet_id": spreadsheet_id,
        "updated_range": updates.get("updatedRange", ""),
        "updated_rows": updates.get("updatedRows", 0),
        "updated_cells": updates.get("updatedCells", 0),
    }
