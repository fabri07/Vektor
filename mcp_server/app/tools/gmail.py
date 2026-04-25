from __future__ import annotations

import base64
import email.mime.text
from typing import Any

import httpx
from fastapi import HTTPException, status

from app.auth.service import get_valid_access_token
from app.security import RequestContext

GMAIL_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"


async def list_messages(
    *,
    session: Any,
    ctx: RequestContext,
    max_results: int = 10,
    query: str | None = None,
    label_ids: list[str] | None = None,
) -> dict[str, Any]:
    access_token, _ = await get_valid_access_token(session, ctx)
    params: dict[str, Any] = {"maxResults": min(max(1, max_results), 50)}
    if query:
        params["q"] = query
    if label_ids:
        params["labelIds"] = label_ids

    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(
            f"{GMAIL_BASE}/messages",
            params=params,
            headers={"Authorization": f"Bearer {access_token}"},
        )

    if resp.status_code == status.HTTP_401_UNAUTHORIZED:
        raise HTTPException(status_code=401, detail="refresh_failed")
    if resp.status_code == 403:
        raise HTTPException(status_code=403, detail="insufficient_scope")
    if resp.status_code >= 400:
        raise HTTPException(status_code=400, detail="gmail_list_failed")

    payload = resp.json()
    message_ids = [m.get("id") for m in payload.get("messages", []) if m.get("id")]
    return {
        "message_ids": message_ids,
        "result_size_estimate": payload.get("resultSizeEstimate", 0),
        "next_page_token": payload.get("nextPageToken"),
    }


async def get_message(
    *,
    session: Any,
    ctx: RequestContext,
    message_id: str,
) -> dict[str, Any]:
    access_token, _ = await get_valid_access_token(session, ctx)

    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(
            f"{GMAIL_BASE}/messages/{message_id}",
            params={"format": "full"},
            headers={"Authorization": f"Bearer {access_token}"},
        )

    if resp.status_code == 401:
        raise HTTPException(status_code=401, detail="refresh_failed")
    if resp.status_code == 403:
        raise HTTPException(status_code=403, detail="insufficient_scope")
    if resp.status_code == 404:
        raise HTTPException(status_code=404, detail="message_not_found")
    if resp.status_code >= 400:
        raise HTTPException(status_code=400, detail="gmail_get_failed")

    msg = resp.json()
    headers_raw = msg.get("payload", {}).get("headers", [])
    headers = {h["name"].lower(): h["value"] for h in headers_raw}

    body = _extract_body(msg.get("payload", {}))

    return {
        "id": msg.get("id"),
        "thread_id": msg.get("threadId"),
        "subject": headers.get("subject", ""),
        "from": headers.get("from", ""),
        "to": headers.get("to", ""),
        "date": headers.get("date", ""),
        "snippet": msg.get("snippet", ""),
        "body_preview": body[:2000],
        "label_ids": msg.get("labelIds", []),
    }


def _extract_body(payload: dict[str, Any]) -> str:
    mime = payload.get("mimeType", "")
    if "text/plain" in mime:
        data = payload.get("body", {}).get("data", "")
        if data:
            return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")

    for part in payload.get("parts", []):
        text = _extract_body(part)
        if text:
            return text
    return ""


async def create_draft(
    *,
    session: Any,
    ctx: RequestContext,
    to: str,
    subject: str,
    body: str,
    cc: str | None = None,
) -> dict[str, Any]:
    access_token, _ = await get_valid_access_token(session, ctx)

    msg = email.mime.text.MIMEText(body, "plain", "utf-8")
    msg["To"] = to
    msg["Subject"] = subject
    if cc:
        msg["Cc"] = cc

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()

    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.post(
            f"{GMAIL_BASE}/drafts",
            json={"message": {"raw": raw}},
            headers={"Authorization": f"Bearer {access_token}"},
        )

    if resp.status_code == 401:
        raise HTTPException(status_code=401, detail="refresh_failed")
    if resp.status_code == 403:
        raise HTTPException(status_code=403, detail="insufficient_scope")
    if resp.status_code >= 400:
        raise HTTPException(status_code=400, detail="gmail_draft_failed")

    draft = resp.json()
    return {
        "draft_id": draft.get("id"),
        "message_id": draft.get("message", {}).get("id"),
        "to": to,
        "subject": subject,
    }
