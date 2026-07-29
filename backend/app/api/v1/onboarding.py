"""Onboarding endpoints."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import get_current_tenant_id
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


# ⚠️ NO gatear este router con `require_open_registration`. La encuesta se partió
# en dos a propósito: el formulario público es de SCREENING (lo lee el dueño para
# decidir la admisión) y los 6 números financieros se piden DESPUÉS de aprobar, en
# el primer login. O sea que `/onboarding/submit` no es una pieza del registro
# abierto: está detrás de JWT, lo usa un usuario ya aprobado y no crea ninguna
# cuenta. Con el flag en su default de producción (`False`), gatearlo dejaría a
# todo tenant aprobado sin poder completar el onboarding y el health score nunca
# se calcularía. Lo fija `test_onboarding_submit_no_esta_gateado_por_el_registro`.
@router.post("/submit", response_model=OnboardingSubmitResponse)
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
