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
    InventoryIntegrityCheckResponse,
    JobStats,
    ObservedMarginDistributionOut,
    RepairRequest,
    VerticalBenchmarkItem,
)
from app.schemas.admin_usage import UsageDashboardResponse

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
    Devuelve el benchmark de margen vigente por vertical, más la distribución observada.

    El benchmark que puntúa es SIEMPRE el estático del JSON del vertical (o el
    override del tenant, que es por-tenant y no aparece en esta vista).

    `observed_distribution` es observación, no benchmark: sirve para detectar
    cuándo un benchmark estático quedó desactualizado, pero no lo reemplaza.
    Su `event_count` cuenta eventos de recálculo, NO negocios distintos —
    `analytics_events` no guarda identificador de negocio, así que un mismo
    negocio recalculado cinco veces cuenta cinco.
    """
    svc = AnalyticsService(session)
    overview = await svc.get_benchmarks_overview()

    items = [
        VerticalBenchmarkItem(
            vertical_code=item["vertical_code"],
            event_count=item["event_count"],
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
            observed_distribution=(
                ObservedMarginDistributionOut(**item["observed_distribution"])  # type: ignore[arg-type]
                if item.get("observed_distribution") is not None
                else None
            ),
        )
        for item in overview
    ]
    return AnalyticsBenchmarksResponse(verticals=items)


# ── Usage / token cost dashboard ──────────────────────────────────────────────


@router.get(
    "/usage",
    response_model=UsageDashboardResponse,
    summary="Consumo de tokens y costo estimado en USD (SUPERADMIN)",
    dependencies=[Depends(require_role("SUPERADMIN"))],
)
async def get_usage_dashboard(
    days: int = Query(default=30, ge=1, le=365, description="Ventana en días (1..365)"),
    tenant_id: UUID | None = Query(
        default=None, description="Filtrar a un tenant; vacío = global (SUPERADMIN ve todo)"
    ),
    session: AsyncSession = Depends(get_db_session),
) -> UsageDashboardResponse:
    """
    Dashboard read-only de consumo de tokens y costo estimado.

    Lee ``decision_audit_log`` (los tokens ya están persistidos). Agrega por
    modelo, agente, día y tenant. Costo estimado con un mapa de precios local
    (⚠️ verificar contra precios oficiales de Anthropic). El SUPERADMIN ve global
    salvo que pase ``tenant_id``.
    """
    from app.application.services import usage_service  # noqa: PLC0415

    return await usage_service.get_usage_dashboard(
        session, days=days, tenant_id=tenant_id
    )


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


@router.get(
    "/inventory-integrity/{tenant_id}",
    response_model=InventoryIntegrityCheckResponse,
    dependencies=[Depends(require_role("SUPERADMIN"))],
    summary="Escaneo read-only de divergencias stock_units vs ledger (no auto-corrige)",
)
async def get_inventory_integrity_check(
    tenant_id: UUID,
    db: AsyncSession = Depends(get_db_session),
) -> InventoryIntegrityCheckResponse:
    """Fase 1 (manual) del chequeo de integridad de inventario: compara
    ``stock_units`` contra ``inicial (catalog_initial_stock) + compras − ventas``
    para productos con ancla confiable. Puramente de lectura — NUNCA escribe
    ``stock_units`` ni persiste una alerta; eso lo hace el job de fase 2
    (``jobs.inventory_integrity_check``) una vez validado el umbral contra datos
    reales via este endpoint.
    """
    from app.application.services.inventory_integrity_service import (  # noqa: PLC0415
        check_tenant_inventory_integrity,
    )

    result = await check_tenant_inventory_integrity(db, tenant_id)
    return InventoryIntegrityCheckResponse.model_validate(result)


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
    "/pipeline/trace/by-file/{file_id}",
    dependencies=[Depends(require_role("SUPERADMIN"))],
    summary="Traza del pipeline a partir del id del archivo (SUPERADMIN)",
)
async def get_pipeline_trace_by_file(
    file_id: UUID,
    db: AsyncSession = Depends(get_db_session),
) -> dict[str, object]:
    """Mismo contenido que ``/pipeline/trace/{trace_id}``, entrando por el archivo.

    Diagnosticar una ingesta arranca SIEMPRE con el ``file_id`` (es lo que se ve
    en la UI y en el log); el ``trace_id`` hay que ir a buscarlo a la base. Este
    atajo evita ese paso. ``UploadedFile.trace_id`` puede ser NULL en uploads
    viejos: ahí el pipeline usa el propio id del archivo como traza (mismo
    fallback que ``confirm_file``), así que se consulta por los dos.
    """
    from app.application.services import pipeline_event_service  # noqa: PLC0415
    from app.persistence.models.file import UploadedFile  # noqa: PLC0415

    record = (
        await db.execute(select(UploadedFile).where(UploadedFile.id == file_id))
    ).scalar_one_or_none()
    if record is None:
        raise HTTPException(status_code=404, detail="Archivo no encontrado.")

    trace_id = record.trace_id or record.id
    events = await pipeline_event_service.get_trace(db, trace_id)
    return {
        "file_id": str(file_id),
        "trace_id": str(trace_id),
        "original_filename": record.original_filename,
        "processing_status": record.processing_status,
        "events": events,
    }


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
