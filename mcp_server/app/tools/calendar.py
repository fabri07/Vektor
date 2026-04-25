from __future__ import annotations

from typing import Any

import httpx
from fastapi import HTTPException, status

from app.auth.service import get_valid_access_token
from app.security import RequestContext

CALENDAR_BASE = "https://www.googleapis.com/calendar/v3"


async def list_events(
    *,
    session: Any,
    ctx: RequestContext,
    calendar_id: str = "primary",
    max_results: int = 10,
    time_min: str | None = None,
    time_max: str | None = None,
) -> dict[str, Any]:
    access_token, _ = await get_valid_access_token(session, ctx)
    params: dict[str, Any] = {
        "maxResults": min(max(1, max_results), 100),
        "singleEvents": "true",
        "orderBy": "startTime",
    }
    if time_min:
        params["timeMin"] = time_min
    if time_max:
        params["timeMax"] = time_max

    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(
            f"{CALENDAR_BASE}/calendars/{calendar_id}/events",
            params=params,
            headers={"Authorization": f"Bearer {access_token}"},
        )

    if resp.status_code == status.HTTP_401_UNAUTHORIZED:
        raise HTTPException(status_code=401, detail="refresh_failed")
    if resp.status_code == 403:
        raise HTTPException(status_code=403, detail="insufficient_scope")
    if resp.status_code >= 400:
        raise HTTPException(status_code=400, detail="calendar_list_failed")

    payload = resp.json()
    events = [
        {
            "id": item.get("id"),
            "summary": item.get("summary", ""),
            "description": item.get("description", ""),
            "start": item.get("start", {}),
            "end": item.get("end", {}),
            "attendees": [a.get("email") for a in item.get("attendees", [])],
            "html_link": item.get("htmlLink"),
        }
        for item in payload.get("items", [])
    ]
    return {"events": events, "calendar_id": calendar_id}


async def create_event(
    *,
    session: Any,
    ctx: RequestContext,
    calendar_id: str = "primary",
    summary: str,
    start: str,
    end: str,
    description: str | None = None,
    attendees: list[str] | None = None,
) -> dict[str, Any]:
    access_token, _ = await get_valid_access_token(session, ctx)

    body: dict[str, Any] = {
        "summary": summary,
        "start": {"dateTime": start} if "T" in start else {"date": start},
        "end": {"dateTime": end} if "T" in end else {"date": end},
    }
    if description:
        body["description"] = description
    if attendees:
        body["attendees"] = [{"email": e} for e in attendees]

    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.post(
            f"{CALENDAR_BASE}/calendars/{calendar_id}/events",
            json=body,
            headers={"Authorization": f"Bearer {access_token}"},
        )

    if resp.status_code == 401:
        raise HTTPException(status_code=401, detail="refresh_failed")
    if resp.status_code == 403:
        raise HTTPException(status_code=403, detail="insufficient_scope")
    if resp.status_code >= 400:
        raise HTTPException(status_code=400, detail="calendar_create_failed")

    event = resp.json()
    return {
        "id": event.get("id"),
        "summary": event.get("summary"),
        "start": event.get("start"),
        "end": event.get("end"),
        "html_link": event.get("htmlLink"),
    }
