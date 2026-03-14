"""
Trigger wrapper for health score recalculation.

Guarantees that only one recalculation runs per tenant at a time
using a Redis SET NX lock (TTL 60 s). If a job is already in flight,
the call is silently ignored to avoid duplicate work.

Usage (from FastAPI endpoint or other async context)
-----------------------------------------------------
    from app.jobs.trigger_recalculation import maybe_trigger_recalculation
    await maybe_trigger_recalculation(tenant_id=str(tenant.tenant_id), redis=redis)
"""

from __future__ import annotations

from uuid import UUID

from app.observability.logger import get_logger

logger = get_logger(__name__)

_LOCK_TTL_SECONDS = 60
_LOCK_KEY_PREFIX = "score_lock:"


async def maybe_trigger_recalculation(
    tenant_id: str | UUID,
    redis: object,
    snapshot_id: str | None = None,
) -> bool:
    """
    Dispatch recalculate_health_score.delay() for *tenant_id* unless a
    recalculation is already in progress (Redis NX lock held).

    Parameters
    ----------
    tenant_id:   Tenant UUID (str or UUID).
    redis:       An async Redis client (redis.asyncio.Redis).
    snapshot_id: Optional BSL snapshot hint forwarded to the task.

    Returns
    -------
    True  — job dispatched.
    False — lock was already held; request silently ignored.
    """
    from app.jobs.recalculate_health_score import recalculate_health_score  # noqa: PLC0415

    tid = str(tenant_id)
    lock_key = f"{_LOCK_KEY_PREFIX}{tid}"

    acquired = await redis.set(lock_key, "1", nx=True, ex=_LOCK_TTL_SECONDS)  # type: ignore[union-attr]
    if not acquired:
        logger.debug(
            "trigger_recalculation.locked",
            tenant_id=tid,
            reason="recalculation already in progress",
        )
        return False

    recalculate_health_score.delay(tid, snapshot_id)
    logger.info("trigger_recalculation.dispatched", tenant_id=tid)
    return True
