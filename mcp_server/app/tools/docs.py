from __future__ import annotations

from typing import Any

import httpx
from fastapi import HTTPException, status

from app.auth.service import get_valid_access_token
from app.security import RequestContext

DOCS_BASE = "https://docs.googleapis.com/v1/documents"


async def create_document(
    *,
    session: Any,
    ctx: RequestContext,
    title: str,
    content: str | None = None,
) -> dict[str, Any]:
    access_token, _ = await get_valid_access_token(session, ctx)

    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.post(
            DOCS_BASE,
            json={"title": title},
            headers={"Authorization": f"Bearer {access_token}"},
        )

    if resp.status_code == status.HTTP_401_UNAUTHORIZED:
        raise HTTPException(status_code=401, detail="refresh_failed")
    if resp.status_code == 403:
        raise HTTPException(status_code=403, detail="insufficient_scope")
    if resp.status_code >= 400:
        raise HTTPException(status_code=400, detail="docs_create_failed")

    doc = resp.json()
    document_id = doc.get("documentId", "")

    if content and document_id:
        await _insert_text(access_token=access_token, document_id=document_id, text=content)

    return {
        "document_id": document_id,
        "title": doc.get("title", title),
        "revision_id": doc.get("revisionId"),
        "url": f"https://docs.google.com/document/d/{document_id}/edit",
    }


async def append_content(
    *,
    session: Any,
    ctx: RequestContext,
    document_id: str,
    content: str,
) -> dict[str, Any]:
    access_token, _ = await get_valid_access_token(session, ctx)
    await _insert_text(access_token=access_token, document_id=document_id, text=content)
    return {
        "document_id": document_id,
        "appended": True,
        "url": f"https://docs.google.com/document/d/{document_id}/edit",
    }


async def _insert_text(*, access_token: str, document_id: str, text: str) -> None:
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.post(
            f"{DOCS_BASE}/{document_id}:batchUpdate",
            json={
                "requests": [
                    {
                        "insertText": {
                            "location": {"index": 1},
                            "text": text,
                        }
                    }
                ]
            },
            headers={"Authorization": f"Bearer {access_token}"},
        )
    if resp.status_code == 401:
        raise HTTPException(status_code=401, detail="refresh_failed")
    if resp.status_code == 403:
        raise HTTPException(status_code=403, detail="insufficient_scope")
    if resp.status_code >= 400:
        raise HTTPException(status_code=400, detail="docs_write_failed")
