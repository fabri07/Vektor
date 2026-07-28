"""Onboarding endpoints."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import get_current_tenant_id, require_open_registration
from app.application.services.onboarding_service import (
    AlreadyOnboardedError,
    OnboardingService,
)
from app.persistence.db.session import get_db_session
from app.schemas.onboarding import (
    OnboardingStatusResponse,
    OnboardingSubmitRequest,
    OnboardingSubmitResponse,
)

router = APIRouter()


@router.post(
    "/submit",
    response_model=OnboardingSubmitResponse,
    # Misma compuerta que `POST /auth/register`: mientras el registro abierto esté
    # apagado, este cuerpo responde 410 `registration_closed`.
    # **`GET /onboarding/status` queda VIVO** a propósito: `/auth/me` y los guards
    # del frontend lo leen para todo tenant ya aprobado.
    dependencies=[Depends(require_open_registration)],
)
async def submit_onboarding(
    body: OnboardingSubmitRequest,
    tenant_id: UUID = Depends(get_current_tenant_id),
    session: AsyncSession = Depends(get_db_session),
) -> OnboardingSubmitResponse:
    svc = OnboardingService(session)
    try:
        return await svc.submit(tenant_id=tenant_id, body=body)
    except AlreadyOnboardedError:
        raise HTTPException(status_code=409, detail="Onboarding already completed.") from None


@router.get("/status", response_model=OnboardingStatusResponse)
async def onboarding_status(
    tenant_id: UUID = Depends(get_current_tenant_id),
    session: AsyncSession = Depends(get_db_session),
) -> OnboardingStatusResponse:
    svc = OnboardingService(session)
    return await svc.get_status(tenant_id=tenant_id)
