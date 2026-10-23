"""
Observability metrics: in-memory counters + DB-persisted activity events.

In-memory counters (fast, lost on restart):
    from app.observability.metrics import increment, get_counters

DB-persisted activity events (survives restarts, queryable):
    from app.observability.metrics import track_event, ActivityEventType

Replace in-memory counters with Prometheus if needed:
    pip install prometheus-fastapi-instrumentator
    from prometheus_fastapi_instrumentator import Instrumentator
    Instrumentator().instrument(app).expose(app)
"""

from collections import defaultdict
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession

# ── In-memory counters ────────────────────────────────────────────────────────

_counters: dict[str, int] = defaultdict(int)


def increment(name: str, labels: dict[str, Any] | None = None) -> None:
    key = name
    if labels:
        label_str = ",".join(f"{k}={v}" for k, v in sorted(labels.items()))
        key = f"{name}{{{label_str}}}"
    _counters[key] += 1


def get_counters() -> dict[str, int]:
    return dict(_counters)


# ── Activity event types ──────────────────────────────────────────────────────


class ActivityEventType(StrEnum):
    # Onboarding funnel
    ONBOARDING_STARTED = "ONBOARDING_STARTED"
    ONBOARDING_COMPLETED = "ONBOARDING_COMPLETED"

    # Core engagement
    FIRST_SCORE_RENDERED = "FIRST_SCORE_RENDERED"
    INSIGHT_VIEWED = "INSIGHT_VIEWED"
    ACTION_ACKNOWLEDGED = "ACTION_ACKNOWLEDGED"

    # Data ingestion
    FILE_UPLOADED = "FILE_UPLOADED"

    # Momentum
    MOMENTUM_WIDGET_VIEWED = "MOMENTUM_WIDGET_VIEWED"
    MILESTONE_UNLOCKED = "MILESTONE_UNLOCKED"

    # Job tracking (SUPERADMIN metrics)
    JOB_SUCCESS = "JOB_SUCCESS"
    JOB_FAILED = "JOB_FAILED"


# ── DB-persisted event tracking ───────────────────────────────────────────────


async def track_event(
    session: AsyncSession,
    tenant_id: UUID,
    event_type: ActivityEventType | str,
    *,
    user_id: UUID | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    """
    Persist an activity event to user_activity_events.
    Fire-and-forget: caller must commit the session or rely on the session
    dependency auto-commit.

    Example:
        await track_event(
            session, tenant_id,
            ActivityEventType.ONBOARDING_COMPLETED,
            user_id=user.user_id,
        )
    """
    from app.persistence.models.activity import UserActivityEvent  # noqa: PLC0415

    event = UserActivityEvent(
        id=uuid4(),
        tenant_id=tenant_id,
        user_id=user_id,
        event_type=str(event_type),
        metadata_json=metadata,
        created_at=datetime.now(UTC),
    )
    session.add(event)
    # increment in-memory counter too
    increment(f"activity.{event_type}", labels={"tenant_id": str(tenant_id)})


async def track_job_event(
    session: AsyncSession,
    job_name: str,
    tenant_id: UUID | None,
    *,
    success: bool,
    duration_ms: int,
    error: str | None = None,
) -> None:
    """
    Persist a job execution event (JOB_SUCCESS or JOB_FAILED).
    Used by SUPERADMIN metrics to compute jobs_last_24h.
    """
    event_type = ActivityEventType.JOB_SUCCESS if success else ActivityEventType.JOB_FAILED
    metadata: dict[str, Any] = {"job_name": job_name, "duration_ms": duration_ms}
    if error:
        metadata["error"] = error

    from app.persistence.models.activity import UserActivityEvent  # noqa: PLC0415

    event = UserActivityEvent(
        id=uuid4(),
        tenant_id=tenant_id or UUID("00000000-0000-0000-0000-000000000000"),
        user_id=None,
        event_type=str(event_type),
        metadata_json=metadata,
        created_at=datetime.now(UTC),
    )
    session.add(event)
    increment(f"job.{event_type.lower()}", labels={"job_name": job_name})


async def record_job_run(
    session: AsyncSession,
    job_name: str,
    *,
    started_at: datetime,
    duration_ms: int,
    success: bool,
    counters: dict[str, Any] | None = None,
    error: str | None = None,
) -> None:
    """Deja la traza durable de UNA ejecución de un job periódico (E5/H16).

    Distinta de :func:`track_job_event`, que escribe en ``user_activity_events``
    y por lo tanto exige un ``tenant_id`` real (FK a ``tenants``). Un job global
    —el barrido de relecturas colgadas recorre TODOS los tenants y en general no
    toca ninguno— no tiene tenant que declarar, y la rama ``tenant_id=None`` de
    aquella función cae al UUID cero, que no existe en ``tenants``.

    **Sólo agrega a la sesión; no comitea.** Así el caller puede escribir la
    traza del éxito en la MISMA transacción que los efectos del job: si el commit
    falla, no queda una traza afirmando un trabajo que no se guardó.

    El ``error`` se trunca acá y no en cada caller: un traceback entero en una
    tabla de observabilidad es una fuga de detalle interno que nadie lee.
    """
    from app.persistence.models.job_run import JobRun  # noqa: PLC0415

    session.add(
        JobRun(
            job_name=job_name,
            started_at=started_at,
            duration_ms=duration_ms,
            success=success,
            counters=counters,
            error=error[:500] if error else None,
        )
    )
