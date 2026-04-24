from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.service import get_status, handle_callback, revoke_auth, start_auth
from app.db import get_db_session
from app.security import RequestContext, require_internal_context

router = APIRouter(prefix="/auth", tags=["auth"])


class AuthStartRequest(BaseModel):
    scopes: list[str] = Field(default_factory=list)


@router.post("/start")
async def auth_start(
    body: AuthStartRequest,
    ctx: RequestContext = Depends(require_internal_context),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    return await start_auth(session, ctx, body.scopes)


@router.get("/callback", response_class=HTMLResponse)
async def auth_callback(
    code: str | None = Query(default=None),
    state: str | None = Query(default=None),
    error: str | None = Query(default=None),
    session: AsyncSession = Depends(get_db_session),
) -> HTMLResponse:
    if error:
        return HTMLResponse(
            f"<html><body><h2>Conexión cancelada</h2><p>{error}</p></body></html>",
            status_code=400,
        )
    if not code or not state:
        raise HTTPException(status_code=400, detail="missing_code_or_state")

    await handle_callback(session, state=state, code=code)
    return HTMLResponse(
        """
        <html>
          <body style="font-family: ui-sans-serif, system-ui; padding: 32px; color: #0f172a;">
            <h2 style="margin: 0 0 12px;">Google conectado</h2>
            <p style="margin: 0 0 16px;">Ya podés volver a Véktor. Esta ventana se puede cerrar.</p>
            <script>setTimeout(() => window.close(), 1200);</script>
          </body>
        </html>
        """.strip()
    )


@router.get("/status")
async def auth_status(
    ctx: RequestContext = Depends(require_internal_context),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    return await get_status(session, ctx)


@router.delete("/revoke")
async def auth_revoke(
    ctx: RequestContext = Depends(require_internal_context),
    session: AsyncSession = Depends(get_db_session),
) -> JSONResponse:
    await revoke_auth(session, ctx)
    return JSONResponse({"ok": True})
