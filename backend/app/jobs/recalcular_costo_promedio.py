"""Celery task: recalcula el costo promedio pendiente de unos productos (M·P2b).

Lo encola ``persistence/costo_promedio_pendiente`` después del commit que dejó los
productos pendientes, en un hilo aparte, una tarea por tenant y tanda. El promedio
es un DATO; el costo principal sigue siendo el de reposición.

1. Recalcula los productos con el contrato de ``costo_promedio_service.recalcular``:
   una foto consistente para el cálculo y una publicación corta que descarta el
   resultado si entró otra marca mientras se calculaba. Si lo descarta, se
   reencola.
2. **Drena** los pendientes viejos del mismo tenant (marcados hace más de
   ``ANTIGUEDAD_DRENAJE``, hasta ``LIMITE_DRENAJE``): un encolado que se perdió
   (broker caído, worker reiniciado entre commit y encolado) lo levanta el
   próximo recálculo del tenant. La red final es el cron
   ``scripts/backfill_costo_promedio.py --pendientes --all --apply``.
3. Deja la corrida en ``job_runs``. Un error no se propaga: queda en
   ``product_cost_state`` (``intentos``, ``ultimo_error``) y la marca sigue
   pendiente.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from app.jobs.celery_app import celery_app
from app.observability.logger import get_logger

logger = get_logger(__name__)

NOMBRE: Final = "jobs.recalcular_costo_promedio"
ANTIGUEDAD_DRENAJE: Final = timedelta(minutes=2)
LIMITE_DRENAJE: Final = 50
MAX_REENCOLADOS: Final = 5


async def procesar(
    fabrica: Any, tenant_id: uuid.UUID, product_ids: list[uuid.UUID]
) -> tuple[bool, dict[str, Any], str | None]:
    """Recalcula los productos, drena los pendientes viejos del tenant y devuelve
    ``(reencolar, contadores, error)``. Un error no se propaga: queda en el
    estado de cada producto (``intentos``, ``ultimo_error``)."""
    from app.application.services import costo_promedio_service as svc  # noqa: PLC0415

    contadores: dict[str, Any] = {}
    error: str | None = None
    reencolar = False
    procesando = list(product_ids)
    try:
        r = await svc.recalcular(fabrica, tenant_id, procesando, solo_pendientes=True)
        reencolar = r.descartados > 0
        contadores.update(publicados=r.publicados, descartados=r.descartados)
        async with fabrica() as sesion:
            viejos = await svc.productos_pendientes(
                sesion,
                tenant_id,
                marcados_antes_de=datetime.now(UTC) - ANTIGUEDAD_DRENAJE,
                limite=LIMITE_DRENAJE,
            )
        procesando = [p for p in viejos if p not in set(product_ids)]
        if procesando:
            d = await svc.recalcular(fabrica, tenant_id, procesando, solo_pendientes=True)
            contadores.update(drenados=d.publicados, drenaje_descartados=d.descartados)
    except Exception as exc:  # noqa: BLE001 — queda registrado y pendiente
        error = f"{type(exc).__name__}: {exc}"
        logger.error(
            "costo_promedio.recalculo_fallo",
            tenant_id=str(tenant_id),
            productos=len(procesando),
            error=error,
        )
        for pid in procesando:
            await svc.registrar_fallo(fabrica, tenant_id, pid, error)
    return reencolar, contadores, error


async def _run(tenant_id_str: str, product_ids: list[str]) -> bool:
    """Devuelve ``True`` si hay que reencolar (algún resultado se descartó)."""
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker  # noqa: PLC0415

    from app.config.settings import get_settings  # noqa: PLC0415
    from app.observability.metrics import record_job_run  # noqa: PLC0415
    from app.persistence.db.engine_factory import crear_engine  # noqa: PLC0415

    s = get_settings()
    engine = crear_engine(s.DATABASE_URL, pool_pre_ping=True, connect_args=s.pg_connect_args)
    fabrica = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    inicio = datetime.now(UTC)
    t0 = time.monotonic()
    try:
        reencolar, contadores, error = await procesar(
            fabrica, uuid.UUID(tenant_id_str), [uuid.UUID(p) for p in product_ids]
        )
        async with fabrica() as sesion:
            await record_job_run(
                sesion,
                NOMBRE,
                started_at=inicio,
                duration_ms=int((time.monotonic() - t0) * 1000),
                success=error is None,
                counters=contadores,
                error=error,
            )
            await sesion.commit()
    finally:
        await engine.dispose()
    return reencolar


@celery_app.task(  # type: ignore[misc]
    bind=True,
    name=NOMBRE,
    max_retries=MAX_REENCOLADOS,
    soft_time_limit=120,
    time_limit=150,
)
def recalcular_costo_promedio(self: Any, tenant_id: str, product_ids: list[str] | str) -> None:
    ids = [product_ids] if isinstance(product_ids, str) else list(product_ids)
    if asyncio.run(_run(tenant_id, ids)) and self.request.retries < MAX_REENCOLADOS:
        # Entró otra marca mientras se calculaba: se vuelve a intentar. Si se
        # agotan los reintentos, la marca queda y la levanta el drenaje.
        raise self.retry(countdown=5 * (self.request.retries + 1))
