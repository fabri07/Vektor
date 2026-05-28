"""Health score endpoints."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import get_current_tenant
from app.persistence.db.session import get_db_session
from app.persistence.models.tenant import Tenant
from app.persistence.repositories.health_score_repository import HealthScoreRepository
from app.schemas.health_score import (
    CalculatingResponse,
    HealthScoreResponse,
    HealthScoreV2Response,
    NoDataResponse,
    ScoreSummaryResponse,
    WeeklyScoreHistoryResponse,
)

router = APIRouter()


class MarginHistoryItem(BaseModel):
    snapshot_date: str
    score_margin: int
    revenue_est: float
    cogs_est: float
    fixed_expenses_est: float
    margin_pct: float


@router.get(
    "/current",
    response_model=HealthScoreResponse | NoDataResponse,
    summary="Get the latest health score for the current tenant",
)
async def get_current_score(
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db_session),
) -> HealthScoreResponse | NoDataResponse:
    from sqlalchemy import func, select as sa_select  # noqa: PLC0415

    from app.persistence.models.transaction import ExpenseEntry, SaleEntry  # noqa: PLC0415

    repo = HealthScoreRepository(session)
    snapshot = await repo.get_latest(tenant.tenant_id)
    if snapshot is None:
        ventas_q = sa_select(func.count(SaleEntry.id)).where(
            SaleEntry.tenant_id == tenant.tenant_id,
            SaleEntry.voided_at.is_(None),
        )
        gastos_q = sa_select(func.count(ExpenseEntry.id)).where(
            ExpenseEntry.tenant_id == tenant.tenant_id,
            ExpenseEntry.voided_at.is_(None),
        )
        data_count = ((await session.scalar(ventas_q)) or 0) + ((await session.scalar(gastos_q)) or 0)
        if data_count == 0 and not tenant.is_demo:
            return NoDataResponse(is_demo_data=False)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No health score available yet. Add some sales or expenses to get started.",
        )
    dimensions: list[object] = snapshot.dimensions if isinstance(snapshot.dimensions, list) else []
    return HealthScoreResponse(
        id=snapshot.id,
        tenant_id=snapshot.tenant_id,
        total_score=snapshot.total_score,
        level=snapshot.level,
        dimensions=dimensions,
        triggered_by=snapshot.triggered_by,
        snapshot_date=snapshot.snapshot_date,
        created_at=snapshot.created_at,
    )


@router.get(
    "/summary",
    response_model=ScoreSummaryResponse,
    summary="Lightweight score summary for dashboard",
)
async def get_score_summary(
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db_session),
) -> ScoreSummaryResponse:
    repo = HealthScoreRepository(session)
    history = await repo.list_by_tenant(tenant.tenant_id, limit=2)
    if not history:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No health score available yet.",
        )
    current = history[0]
    previous = history[1] if len(history) > 1 else None
    delta = (current.total_score - previous.total_score) if previous else None
    return ScoreSummaryResponse(
        current_score=current.total_score,
        level=current.level,
        previous_score=previous.total_score if previous else None,
        delta=delta,
        snapshot_date=current.snapshot_date,
        needs_attention=current.level in ("critical", "warning"),
    )


@router.get(
    "/history",
    response_model=list[HealthScoreResponse],
    summary="Get historical health scores",
)
async def get_score_history(
    limit: int = Query(default=30, ge=1, le=90),
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db_session),
) -> list[HealthScoreResponse]:
    repo = HealthScoreRepository(session)
    snapshots = await repo.list_by_tenant(tenant.tenant_id, limit=limit)
    return [
        HealthScoreResponse(
            id=s.id,
            tenant_id=s.tenant_id,
            total_score=s.total_score,
            level=s.level,
            dimensions=s.dimensions if isinstance(s.dimensions, list) else [],
            triggered_by=s.triggered_by,
            snapshot_date=s.snapshot_date,
            created_at=s.created_at,
        )
        for s in snapshots
    ]


@router.get(
    "/weekly",
    response_model=list[WeeklyScoreHistoryResponse],
    summary="Weekly score history for trend charts",
)
async def get_weekly_history(
    limit: int = Query(default=12, ge=1, le=52),
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db_session),
) -> list[WeeklyScoreHistoryResponse]:
    repo = HealthScoreRepository(session)
    return await repo.get_weekly_history(tenant.tenant_id, limit=limit)  # type: ignore[return-value]


@router.get(
    "/latest",
    response_model=HealthScoreV2Response | CalculatingResponse | NoDataResponse,
    summary="Latest health score with explicit subscores (F1-01)",
)
async def get_latest_score(
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db_session),
) -> HealthScoreV2Response | CalculatingResponse | NoDataResponse:
    """
    Returns the most recent HealthScoreSnapshot with all F1-01 subscore columns.
    - CALCULATING: score existe pero aún no tiene subscores calculados.
    - NO_DATA: tenant real sin ventas ni gastos cargados.
    - is_demo_data=true: el snapshot pertenece a un tenant demo.
    """
    from sqlalchemy import func, select as sa_select  # noqa: PLC0415

    from app.persistence.models.transaction import ExpenseEntry, SaleEntry  # noqa: PLC0415

    repo = HealthScoreRepository(session)
    snapshot = await repo.get_latest(tenant.tenant_id)

    # Sin snapshot: verificar si hay datos del tenant para decidir entre CALCULATING y NO_DATA.
    if snapshot is None or snapshot.score_cash is None:
        ventas_q = sa_select(func.count(SaleEntry.id)).where(
            SaleEntry.tenant_id == tenant.tenant_id,
            SaleEntry.voided_at.is_(None),
        )
        gastos_q = sa_select(func.count(ExpenseEntry.id)).where(
            ExpenseEntry.tenant_id == tenant.tenant_id,
            ExpenseEntry.voided_at.is_(None),
        )
        data_count = ((await session.scalar(ventas_q)) or 0) + ((await session.scalar(gastos_q)) or 0)
        if data_count == 0 and not tenant.is_demo:
            # Tenant real sin ningún dato cargado — estado nulo explícito.
            return NoDataResponse(is_demo_data=False)
        return CalculatingResponse()

    return HealthScoreV2Response(
        id=snapshot.id,
        tenant_id=snapshot.tenant_id,
        score_total=int(snapshot.total_score),
        score_cash=snapshot.score_cash,
        score_margin=snapshot.score_margin,
        score_stock=snapshot.score_stock,
        score_supplier=snapshot.score_supplier,
        score_growth=snapshot.score_growth,      # None si es snapshot v1
        primary_risk_code=snapshot.primary_risk_code or "",
        confidence_level=snapshot.confidence_level or "",
        data_completeness_score=float(snapshot.data_completeness_score or 0),
        level=snapshot.level,
        created_at=snapshot.created_at,
        is_demo_data=tenant.is_demo,
    )


@router.get(
    "/margin-history",
    response_model=list[MarginHistoryItem],
    summary="Historial de componentes de margen por snapshot",
)
async def get_margin_history(
    limit: int = Query(default=12, ge=1, le=24),
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db_session),
) -> list[MarginHistoryItem]:
    """
    Retorna los últimos N snapshots con composición del margen.
    Lee monthly_sales_est / cogs_est / fixed_expenses_est desde score_inputs_json.
    No requiere tabla nueva — los datos ya están en el snapshot.
    """
    repo = HealthScoreRepository(session)
    snapshots = await repo.list_by_tenant(tenant.tenant_id, limit=limit)

    result = []
    for snap in snapshots:
        inputs: dict = snap.score_inputs_json or {}
        revenue = float(str(inputs.get("monthly_sales_est") or 0))
        cogs = float(str(inputs.get("monthly_inventory_cost_est") or 0))
        fixed = float(str(inputs.get("monthly_fixed_expenses_est") or 0))
        margin_pct = round((revenue - cogs - fixed) / revenue * 100, 1) if revenue > 0 else 0.0
        result.append(
            MarginHistoryItem(
                snapshot_date=snap.snapshot_date.isoformat(),
                score_margin=snap.score_margin or 0,
                revenue_est=revenue,
                cogs_est=cogs,
                fixed_expenses_est=fixed,
                margin_pct=margin_pct,
            )
        )
    return result


@router.get(
    "/history/v2",
    response_model=list[HealthScoreV2Response],
    summary="Last 12 health scores with explicit subscores (F1-01)",
)
async def get_score_history_v2(
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db_session),
) -> list[HealthScoreV2Response]:
    """Returns the last 12 health score snapshots for trend charts."""
    repo = HealthScoreRepository(session)
    snapshots = await repo.list_by_tenant(tenant.tenant_id, limit=12)
    return [
        HealthScoreV2Response(
            id=s.id,
            tenant_id=s.tenant_id,
            score_total=int(s.total_score),
            score_cash=s.score_cash or 0,
            score_margin=s.score_margin or 0,
            score_stock=s.score_stock or 0,
            score_supplier=s.score_supplier or 0,
            score_growth=s.score_growth,          # None si es snapshot v1
            primary_risk_code=s.primary_risk_code or "",
            confidence_level=s.confidence_level or "",
            data_completeness_score=float(s.data_completeness_score or 0),
            level=s.level,
            created_at=s.created_at,
        )
        for s in snapshots
        if s.score_cash is not None  # only F1-01 snapshots
    ]


class ExportRequest(BaseModel):
    format: str = Field("pdf", pattern="^(pdf|docx)$")
    narrative: str = Field("", max_length=4000)


@router.post(
    "/{snapshot_id}/export",
    summary="Exportar informe de salud como PDF o DOCX",
)
async def export_health_report(
    snapshot_id: uuid.UUID,
    body: ExportRequest,
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db_session),
) -> Response:
    """Genera y descarga el informe de salud financiera en PDF o DOCX.

    Body JSON:
    - format: 'pdf' (default) o 'docx'.
    - narrative: texto del análisis ejecutivo (max 4000 chars; opcional).
    """
    from app.application.services.report_export_service import generate_docx, generate_pdf  # noqa: PLC0415

    repo = HealthScoreRepository(session)
    snapshot = await repo.get_by_id(snapshot_id)
    if snapshot is None or snapshot.tenant_id != tenant.tenant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Snapshot no encontrado.")

    business_name = tenant.display_name or "Mi negocio"

    if body.format == "pdf":
        content = generate_pdf(snapshot, body.narrative, business_name)
        media_type = "application/pdf"
        filename = f"informe-salud-{snapshot.snapshot_date.strftime('%Y%m%d')}.pdf"
    else:
        content = generate_docx(snapshot, body.narrative, business_name)
        media_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        filename = f"informe-salud-{snapshot.snapshot_date.strftime('%Y%m%d')}.docx"

    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
