from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db_session
from app.security import RequestContext, require_internal_context
from app.tools import calendar, docs, drive, gmail, sheets

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


_ALL_TOOLS = [
    "google.drive.list_files",
    "google.drive.read_file",
    "google.drive.upload_file",
    "google.gmail.list_messages",
    "google.gmail.get_message",
    "google.gmail.create_draft",
    "google.calendar.list_events",
    "google.calendar.create_event",
    "google.sheets.read_range",
    "google.sheets.append_rows",
    "google.docs.create_document",
    "google.docs.append_content",
]


@router.get("/tools/list")
async def list_tools() -> dict[str, Any]:
    return {"tools": [{"name": name} for name in _ALL_TOOLS]}


@router.post("/tools/call")
async def call_tool(
    body: JsonRpcRequest,
    ctx: RequestContext = Depends(require_internal_context),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    if body.method != "tools/call":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="unsupported_method")

    name = body.params.name
    args = body.params.arguments

    try:
        # --- Drive ---
        if name == "google.drive.list_files":
            result = await drive.list_files(
                session=session, ctx=ctx,
                folder_id=args.get("folder_id"),
                query=args.get("query"),
                max_results=int(args.get("max_results", 10)),
            )
            return _ok(result, body.id)

        if name == "google.drive.read_file":
            file_id = str(args.get("file_id", "")).strip()
            if not file_id:
                return _error("validation_error", "file_id es requerido", body.id)
            result = await drive.read_file(session=session, ctx=ctx, file_id=file_id)
            return _ok(result, body.id)

        if name == "google.drive.upload_file":
            name_ = str(args.get("name", "")).strip()
            content_b64 = str(args.get("content_base64", "")).strip()
            mime_type = str(args.get("mime_type", "")).strip()
            if not name_ or not content_b64 or not mime_type:
                return _error("validation_error", "name, content_base64 y mime_type son requeridos", body.id)
            result = await drive.upload_file(
                session=session, ctx=ctx,
                name=name_, content_base64=content_b64,
                mime_type=mime_type, folder_id=args.get("folder_id"),
            )
            return _ok(result, body.id)

        # --- Gmail ---
        if name == "google.gmail.list_messages":
            result = await gmail.list_messages(
                session=session, ctx=ctx,
                max_results=int(args.get("max_results", 10)),
                query=args.get("query"),
                label_ids=args.get("label_ids"),
            )
            return _ok(result, body.id)

        if name == "google.gmail.get_message":
            message_id = str(args.get("message_id", "")).strip()
            if not message_id:
                return _error("validation_error", "message_id es requerido", body.id)
            result = await gmail.get_message(session=session, ctx=ctx, message_id=message_id)
            return _ok(result, body.id)

        if name == "google.gmail.create_draft":
            to = str(args.get("to", "")).strip()
            subject = str(args.get("subject", "")).strip()
            body_text = str(args.get("body", "")).strip()
            if not to or not subject or not body_text:
                return _error("validation_error", "to, subject y body son requeridos", body.id)
            result = await gmail.create_draft(
                session=session, ctx=ctx,
                to=to, subject=subject, body=body_text,
                cc=args.get("cc"),
            )
            return _ok(result, body.id)

        # --- Calendar ---
        if name == "google.calendar.list_events":
            result = await calendar.list_events(
                session=session, ctx=ctx,
                calendar_id=str(args.get("calendar_id", "primary")),
                max_results=int(args.get("max_results", 10)),
                time_min=args.get("time_min"),
                time_max=args.get("time_max"),
            )
            return _ok(result, body.id)

        if name == "google.calendar.create_event":
            summary = str(args.get("summary", "")).strip()
            start = str(args.get("start", "")).strip()
            end = str(args.get("end", "")).strip()
            if not summary or not start or not end:
                return _error("validation_error", "summary, start y end son requeridos", body.id)
            result = await calendar.create_event(
                session=session, ctx=ctx,
                calendar_id=str(args.get("calendar_id", "primary")),
                summary=summary, start=start, end=end,
                description=args.get("description"),
                attendees=args.get("attendees"),
            )
            return _ok(result, body.id)

        # --- Sheets ---
        if name == "google.sheets.read_range":
            spreadsheet_id = str(args.get("spreadsheet_id", "")).strip()
            range_ = str(args.get("range", "")).strip()
            if not spreadsheet_id or not range_:
                return _error("validation_error", "spreadsheet_id y range son requeridos", body.id)
            result = await sheets.read_range(
                session=session, ctx=ctx,
                spreadsheet_id=spreadsheet_id, range_=range_,
            )
            return _ok(result, body.id)

        if name == "google.sheets.append_rows":
            spreadsheet_id = str(args.get("spreadsheet_id", "")).strip()
            range_ = str(args.get("range", "")).strip()
            values = args.get("values", [])
            if not spreadsheet_id or not range_ or not values:
                return _error("validation_error", "spreadsheet_id, range y values son requeridos", body.id)
            result = await sheets.append_rows(
                session=session, ctx=ctx,
                spreadsheet_id=spreadsheet_id, range_=range_, values=values,
            )
            return _ok(result, body.id)

        # --- Docs ---
        if name == "google.docs.create_document":
            title = str(args.get("title", "")).strip()
            if not title:
                return _error("validation_error", "title es requerido", body.id)
            result = await docs.create_document(
                session=session, ctx=ctx,
                title=title, content=args.get("content"),
            )
            return _ok(result, body.id)

        if name == "google.docs.append_content":
            document_id = str(args.get("document_id", "")).strip()
            content = str(args.get("content", "")).strip()
            if not document_id or not content:
                return _error("validation_error", "document_id y content son requeridos", body.id)
            result = await docs.append_content(
                session=session, ctx=ctx,
                document_id=document_id, content=content,
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
