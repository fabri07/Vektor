from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db_session
from app.security import RequestContext, require_internal_context
from app.tools import drive

router = APIRouter(tags=["tools"])


class JsonRpcToolParams(BaseModel):
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = None


class JsonRpcRequest(BaseModel):
    jsonrpc: str
    id: str | int | None = None
    method: str
    params: JsonRpcToolParams


def _ok(result: dict[str, Any], request_id: str | int | None) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": {"isError": False, **result}}


def _error(error_code: str, message: str, request_id: str | int | None) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": {"isError": True, "errorCode": error_code, "message": message},
    }


@router.get("/tools/list")
async def list_tools() -> dict[str, Any]:
    return {
        "tools": [
            {"name": "google.drive.list_files"},
            {"name": "google.drive.read_file"},
            {"name": "google.drive.upload_file"},
        ]
    }


@router.post("/tools/call")
async def call_tool(
    body: JsonRpcRequest,
    ctx: RequestContext = Depends(require_internal_context),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    if body.method != "tools/call":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="unsupported_method")

    try:
        if body.params.name == "google.drive.list_files":
            result = await drive.list_files(
                session=session,
                ctx=ctx,
                folder_id=body.params.arguments.get("folder_id"),
                query=body.params.arguments.get("query"),
                max_results=int(body.params.arguments.get("max_results", 10)),
            )
            return _ok(result, body.id)

        if body.params.name == "google.drive.read_file":
            file_id = str(body.params.arguments.get("file_id", "")).strip()
            if not file_id:
                return _error("validation_error", "file_id es requerido", body.id)
            result = await drive.read_file(session=session, ctx=ctx, file_id=file_id)
            return _ok(result, body.id)

        if body.params.name == "google.drive.upload_file":
            name = str(body.params.arguments.get("name", "")).strip()
            content_base64 = str(body.params.arguments.get("content_base64", "")).strip()
            mime_type = str(body.params.arguments.get("mime_type", "")).strip()
            if not name or not content_base64 or not mime_type:
                return _error("validation_error", "name, content_base64 y mime_type son requeridos", body.id)
            result = await drive.upload_file(
                session=session,
                ctx=ctx,
                name=name,
                content_base64=content_base64,
                mime_type=mime_type,
                folder_id=body.params.arguments.get("folder_id"),
            )
            return _ok(result, body.id)
    except HTTPException as exc:
        detail = str(exc.detail)
        if detail in {"not_connected", "refresh_failed", "insufficient_scope"}:
            return _error(detail, detail, body.id)
        if detail == "validation_error":
            return _error("validation_error", detail, body.id)
        if exc.status_code == 404:
            raise
        return _error("unknown", detail, body.id)

    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="tool_not_found")
