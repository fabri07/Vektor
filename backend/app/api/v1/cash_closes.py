"""Cash close (arqueo de caja diario) endpoints. Sprint 20."""

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import get_current_tenant, require_role
from app.application.services.cash_close_service import (
    CashCloseAlreadyExistsError,
    CashCloseService,
)
from app.persistence.db.session import get_db_session
from app.persistence.models.tenant import Tenant
from app.persistence.models.user import User
from app.schemas.cash_close import (
    CashClosePreviewResponse,
    CashCloseResponse,
    CreateCashCloseRequest,
)

router = APIRouter()


@router.get("/preview", response_model=CashClosePreviewResponse, summary="Cash close preview")
async def cash_close_preview(
    close_date: date = Query(...),
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db_session),
) -> CashClosePreviewResponse:
    """Esperado del día por método (con neteo de efectivo) + estado del cierre."""
    svc = CashCloseService(session)
    return await svc.preview(tenant.tenant_id, close_date)


@router.post(
    "",
    response_model=CashCloseResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Register a daily cash close",
)
async def create_cash_close(
    body: CreateCashCloseRequest,
    current_user: User = Depends(require_role("OWNER", "ADMIN")),
    session: AsyncSession = Depends(get_db_session),
) -> CashCloseResponse:
    svc = CashCloseService(session)
    try:
        return await svc.create(current_user.tenant_id, current_user.user_id, body)
    except CashCloseAlreadyExistsError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Ya registraste el cierre de caja de ese día.",
        ) from exc


@router.get("", response_model=list[CashCloseResponse], summary="List cash closes")
async def list_cash_closes(
    from_date: date = Query(...),
    to_date: date = Query(...),
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db_session),
) -> list[CashCloseResponse]:
    svc = CashCloseService(session)
    return await svc.list_closes(tenant.tenant_id, from_date, to_date)
