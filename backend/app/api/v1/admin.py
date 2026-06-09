"""
SUPERADMIN metrics y analytics endpoints.

GET /api/v1/admin/metrics           — estadísticas de la plataforma
GET /api/v1/admin/analytics/benchmarks — benchmarks por vertical (data moat)
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import require_role
from app.application.services.analytics_service import AnalyticsService
from app.persistence.db.session import get_db_session
from app.persistence.models.activity import UserActivityEvent
from app.persistence.models.business import BusinessProfile, BusinessSnapshot
from app.persistence.models.score import HealthScoreSnapshot
from app.persistence.models.tenant import Tenant
from app.schemas.admin import (
    AdminMetricsResponse,
    AnalyticsBenchmarksResponse,
    BenchmarkThresholds,
    DataRepairItemResponse,
    DataRepairRunResponse,
    JobStats,
    RepairRequest,
    VerticalBenchmarkItem,
)

router = APIRouter()


@router.get(
    "/metrics",
    response_model=AdminMetricsResponse,
    summary="Platform-wide metrics (SUPERADMIN only)",
    dependencies=[Depends(require_role("SUPERADMIN"))],
)
async def get_admin_metrics(
    session: AsyncSession = Depends(get_db_session),
) -> AdminMetricsResponse:
    # 1. total_tenants
    total_tenants: int = (await session.scalar(select(func.count()).select_from(Tenant))) or 0

    # 2. total_onboarding_completed
    total_onboarding: int = (
        await session.scalar(
            select(func.count())
            .select_from(BusinessProfile)
            .where(BusinessProfile.onboarding_completed.is_(True))
        )
    ) or 0

    # 3. avg_data_completeness_score — latest snapshot per tenant
    latest_snapshot_subq = (
        select(
            BusinessSnapshot.tenant_id,
            func.max(BusinessSnapshot.snapshot_date).label("max_date"),
        )
        .group_by(BusinessSnapshot.tenant_id)
        .subquery()
    )
    avg_completeness: float | None = await session.scalar(
        select(func.avg(BusinessSnapshot.data_completeness_score)).where(
            BusinessSnapshot.tenant_id == latest_snapshot_subq.c.tenant_id,
            BusinessSnapshot.snapshot_date == latest_snapshot_subq.c.max_date,
        )
    )

    # 4. avg_health_score — latest snapshot per tenant
    latest_score_subq = (
        select(
            HealthScoreSnapshot.tenant_id,
            func.max(HealthScoreSnapshot.snapshot_date).label("max_date"),
        )
        .group_by(HealthScoreSnapshot.tenant_id)
        .subquery()
    )
    avg_health: float | None = await session.scalar(
        select(func.avg(HealthScoreSnapshot.total_score)).where(
            HealthScoreSnapshot.tenant_id == latest_score_subq.c.tenant_id,
            HealthScoreSnapshot.snapshot_date == latest_score_subq.c.max_date,
        )
    )

    # 5. jobs_last_24h
    cutoff = datetime.now(UTC) - timedelta(hours=24)
    job_success: int = (
        await session.scalar(
            select(func.count())
            .select_from(UserActivityEvent)
            .where(
                UserActivityEvent.event_type == "JOB_SUCCESS",
                UserActivityEvent.created_at >= cutoff,
            )
        )
    ) or 0
    job_failed: int = (
        await session.scalar(
            select(func.count())
            .select_from(UserActivityEvent)
            .where(
                UserActivityEvent.event_type == "JOB_FAILED",
                UserActivityEvent.created_at >= cutoff,
            )
        )
    ) or 0

    # 6. tenants_by_vertical
    rows = (
        await session.execute(
            select(BusinessProfile.vertical_code, func.count().label("cnt")).group_by(
                BusinessProfile.vertical_code
            )
        )
    ).all()
    tenants_by_vertical: dict[str, int] = {row.vertical_code: row.cnt for row in rows}

    return AdminMetricsResponse(
        total_tenants=total_tenants,
        total_onboarding_completed=total_onboarding,
        avg_data_completeness_score=(
            round(float(avg_completeness), 2) if avg_completeness is not None else None
        ),
        avg_health_score=(round(float(avg_health), 2) if avg_health is not None else None),
        jobs_last_24h=JobStats(success=job_success, failed=job_failed),
        tenants_by_vertical=tenants_by_vertical,
    )


@router.get(
    "/analytics/benchmarks",
    response_model=AnalyticsBenchmarksResponse,
    summary="Benchmarks por vertical calculados desde datos reales (SUPERADMIN)",
    dependencies=[Depends(require_role("SUPERADMIN"))],
)
async def get_analytics_benchmarks(
    session: AsyncSession = Depends(get_db_session),
) -> AnalyticsBenchmarksResponse:
    """
    Devuelve los benchmarks de margen vigentes por vertical.

    - Si hay >= 5 muestras en analytics_events (últimos 90 días): benchmark data-driven
      calculado con percentiles p10/p25/p50/p75 del margin_ratio real.
    - Si no hay suficientes datos: benchmark estático del JSON del vertical.

    Permite a Véktor monitorear cuándo los benchmarks estáticos quedan desactualizados.
    """
    svc = AnalyticsService(session)
    overview = await svc.get_benchmarks_overview()

    items = [
        VerticalBenchmarkItem(
            vertical_code=item["vertical_code"],
            sample_count=item["sample_count"],
            avg_score=item.get("avg_score"),
            avg_margin_ratio=item.get("avg_margin_ratio"),
            p50_margin_ratio=item.get("p50_margin_ratio"),
            avg_data_completeness=item.get("avg_data_completeness"),
            benchmark_source=item["benchmark_source"],
            benchmark=BenchmarkThresholds(
                critical_below=item["benchmark"]["critical_below"],  # type: ignore[index]
                warning_below=item["benchmark"]["warning_below"],  # type: ignore[index]
                healthy_min=item["benchmark"]["healthy_min"],  # type: ignore[index]
                healthy_max=item["benchmark"]["healthy_max"],  # type: ignore[index]
                source=item["benchmark_source"],
            ),
        )
        for item in overview
    ]
    return AnalyticsBenchmarksResponse(verticals=items)


# ── Data repair endpoints ─────────────────────────────────────────────────────


@router.post(
    "/repairs/misclassified-product-imports/dry-run",
    response_model=DataRepairRunResponse,
    dependencies=[Depends(require_role("SUPERADMIN"))],
    summary="Detecta importaciones mal clasificadas (dry-run persistente, sin modificar datos)",
)
async def repair_dry_run(
    body: RepairRequest,
    db: AsyncSession = Depends(get_db_session),
) -> DataRepairRunResponse:
    from app.application.services.data_repair_service import apply_repair  # noqa: PLC0415
    from app.persistence.models.repair import DataRepairRun  # noqa: PLC0415

    result = await apply_repair(
        db,
        tenant_id=body.tenant_id,
        dry_run=True,
        source_run_id=body.source_run_id,
    )
    await db.commit()
    run_res = await db.get(DataRepairRun, result.run_id)
    if run_res is None:
        raise HTTPException(status_code=500, detail="Run not found after commit")
    return DataRepairRunResponse.model_validate(run_res)


@router.post(
    "/repairs/misclassified-product-imports/apply",
    response_model=DataRepairRunResponse,
    dependencies=[Depends(require_role("SUPERADMIN"))],
    summary="Aplica la reparación: anula ventas incorrectas y crea productos",
)
async def repair_apply(
    body: RepairRequest,
    db: AsyncSession = Depends(get_db_session),
) -> DataRepairRunResponse:
    from app.application.services.data_repair_service import apply_repair  # noqa: PLC0415
    from app.persistence.models.repair import DataRepairRun  # noqa: PLC0415

    result = await apply_repair(
        db,
        tenant_id=body.tenant_id,
        dry_run=False,
        source_run_id=body.source_run_id,
    )
    await db.commit()
    run_res = await db.get(DataRepairRun, result.run_id)
    if run_res is None:
        raise HTTPException(status_code=500, detail="Run not found after commit")
    return DataRepairRunResponse.model_validate(run_res)


@router.get(
    "/repairs/{run_id}",
    response_model=DataRepairRunResponse,
    dependencies=[Depends(require_role("SUPERADMIN"))],
    summary="Detalle de un DataRepairRun",
)
async def get_repair_run(
    run_id: UUID,
    db: AsyncSession = Depends(get_db_session),
) -> DataRepairRunResponse:
    from app.persistence.models.repair import DataRepairRun  # noqa: PLC0415

    run = await db.get(DataRepairRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Repair run not found")
    return DataRepairRunResponse.model_validate(run)


@router.get(
    "/repairs/{run_id}/items",
    response_model=list[DataRepairItemResponse],
    dependencies=[Depends(require_role("SUPERADMIN"))],
    summary="Items paginados de un DataRepairRun",
)
async def get_repair_run_items(
    run_id: UUID,
    limit: int = Query(default=100, le=500),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db_session),
) -> list[DataRepairItemResponse]:
    from app.persistence.models.repair import DataRepairItem  # noqa: PLC0415

    result = await db.execute(
        select(DataRepairItem)
        .where(DataRepairItem.run_id == run_id)
        .order_by(DataRepairItem.created_at)
        .limit(limit)
        .offset(offset)
    )
    items = list(result.scalars().all())
    return [DataRepairItemResponse.model_validate(item) for item in items]


# ── FASE 0: observabilidad del pipeline de ingestión ──────────────────────────


@router.get(
    "/pipeline/stats",
    dependencies=[Depends(require_role("SUPERADMIN"))],
    summary="Throughput, tasa de rechazo y latencia P95 del pipeline por stage (SUPERADMIN)",
)
async def get_pipeline_stats(
    since: str | None = Query(
        default=None, description="ISO date/datetime; default = últimos 7 días"
    ),
    db: AsyncSession = Depends(get_db_session),
) -> dict[str, object]:
    from app.application.services import pipeline_event_service  # noqa: PLC0415

    since_value = since or (datetime.now(UTC) - timedelta(days=7)).isoformat()
    stages = await pipeline_event_service.get_stats(db, since_value)
    return {"since": since_value, "stages": stages}


@router.get(
    "/pipeline/trace/{trace_id}",
    dependencies=[Depends(require_role("SUPERADMIN"))],
    summary="Ciclo completo (upload→parse→validate→confirm) de un upload (SUPERADMIN)",
)
async def get_pipeline_trace(
    trace_id: UUID,
    db: AsyncSession = Depends(get_db_session),
) -> dict[str, object]:
    from app.application.services import pipeline_event_service  # noqa: PLC0415

    events = await pipeline_event_service.get_trace(db, trace_id)
    if not events:
        raise HTTPException(status_code=404, detail="trace_id no encontrado.")
    return {"trace_id": str(trace_id), "events": events}
