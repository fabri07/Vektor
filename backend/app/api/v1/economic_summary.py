"""FASE 4: endpoint del resumen económico analítico (NO contable)."""

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import get_current_tenant
from app.application.services.economic_summary_service import compute_economic_summary
from app.persistence.db.session import get_db_session
from app.persistence.models.tenant import Tenant
from app.schemas.economic_summary import EconomicSummaryResponse

router = APIRouter()


@router.get(
    "",
    response_model=EconomicSummaryResponse,
    summary="Resumen económico analítico para un rango de fechas",
)
async def get_economic_summary(
    from_date: date = Query(..., description="Inicio del período (YYYY-MM-DD)"),
    to_date: date = Query(..., description="Fin del período (YYYY-MM-DD)"),
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db_session),
) -> EconomicSummaryResponse:
    """Ingresos, egresos, resultado y stock valorizado del período.

    Resumen gerencial analítico, NO contable: no incluye activos/pasivos/patrimonio
    ni estados contables. El stock se valúa con `stock_units × unit_cost_ars`; los
    productos sin costo no suman y se reportan en `missing_cost_*`.
    """
    if from_date > to_date:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="from_date no puede ser posterior a to_date.",
        )
    data = await compute_economic_summary(session, tenant.tenant_id, from_date, to_date)
    return EconomicSummaryResponse(**data)
